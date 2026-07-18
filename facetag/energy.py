"""Per-clip "energy" scoring: how much excitement / action / emotional
intensity a clip carries, so Spotted can auto-tag it high/medium/low and drop
markers on its peak moments (the bits that matter in a recap).

Two signals, both from tools already bundled in the sidecar (ffmpeg + OpenCV):

- **Audio loudness envelope** (ffmpeg): cheering, laughter, music and raised
  voices read as high energy; near-silence reads as low / somber. This is the
  strongest single cue and works even when the shot is static.
- **Camera-compensated motion** (OpenCV optical flow): estimate the global
  camera motion between frames and subtract it, so what's left is *subject*
  movement. A shaky handheld pan over a still scene scores low; kids running
  across the frame scores high. This is the "movement that isn't just camera
  shake" distinction the feature was asked for.

Both signals are reduced to a common 1-second grid, combined into a 0..1 energy
series, aggregated to one clip score + bucket, and reduced to a few peak
timestamps for timeline markers.

No heavy imports at module load — cv2 is imported lazily inside the motion pass,
ffmpeg/ffprobe are called as subprocesses. So importing this module never drags
in InsightFace or Core ML.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --- tunables --------------------------------------------------------------
# These map raw measurements onto 0..1. Defaults are calibrated on real home
# footage (see scripts/energy_calibrate.py); recording levels vary, so the
# audio floor/ceiling are the knobs most likely to need per-library tuning.
AUDIO_FLOOR_DBFS = -55.0   # at/below this RMS -> 0 audio energy (near silence)
AUDIO_CEIL_DBFS = -14.0    # at/above this RMS -> 1 audio energy (loud)
MOTION_FLOOR = 0.30        # subject-motion residual (px/frame) -> 0
MOTION_CEIL = 6.0          # subject-motion residual (px/frame) -> 1
# Audio and motion combine as a soft-OR (noisy-OR), not an average: a clip is
# exciting if it's loud OR moving, and both together boosts. Crucially a
# missing or silent audio track then contributes 0 without dragging the score
# down (much home/B-roll footage has no usable audio).

# Clip aggregate -> bucket. The aggregate is the mean of the top 30% of the
# per-second series ("how intense are the peak moments"), which suits recaps:
# a mostly-calm clip with one real cheer should not read "low".
BUCKET_HIGH = 0.52
BUCKET_LOW = 0.24

# Peak-marker detection on the per-second series.
PEAK_MIN = 0.50            # a peak must reach at least this energy
PEAK_MIN_GAP_SEC = 3       # don't place two markers closer than this
PEAK_MAX = 6               # cap markers per clip

BUCKETS = ("low", "medium", "high")


@dataclass
class EnergyResult:
    score: float                                       # 0..1 clip aggregate
    bucket: str                                        # "high"|"medium"|"low"
    series: np.ndarray                                 # per-second 0..1 energy
    peaks: list[float] = field(default_factory=list)   # peak timestamps (sec)
    have_audio: bool = True
    have_motion: bool = True

    @property
    def keyword(self) -> str:
        """The tag written into the clip, e.g. 'high energy'."""
        return f"{self.bucket} energy"


# --- ffprobe / ffmpeg helpers ---------------------------------------------
def _duration(video_path: Path) -> float:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not on PATH")
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(video_path)],
        stderr=subprocess.DEVNULL,
    )
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def _probe_wh(video_path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video_path)],
        stderr=subprocess.DEVNULL,
    )
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


# --- audio energy ----------------------------------------------------------
def audio_dbfs(video_path: Path, hop_sec: float = 1.0, sr: int = 16000) -> np.ndarray:
    """Per-`hop_sec` RMS loudness in dBFS. Empty array if the clip has no audio.

    Decodes to mono PCM once and windows it — cheap (no image work) and gives a
    clean per-second loudness envelope we control end to end.
    """
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True,
    )
    raw = proc.stdout
    if len(raw) < 2:
        return np.empty(0, dtype=np.float32)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    hop = max(1, int(sr * hop_sec))
    n = len(x) // hop
    if n == 0:
        return np.empty(0, dtype=np.float32)
    frames = x[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    return (20.0 * np.log10(rms + 1e-9)).astype(np.float32)


def _audio01(dbfs: np.ndarray) -> np.ndarray:
    span = AUDIO_CEIL_DBFS - AUDIO_FLOOR_DBFS
    return np.clip((dbfs - AUDIO_FLOOR_DBFS) / span, 0.0, 1.0).astype(np.float32)


# --- motion energy (camera-compensated) -----------------------------------
def _iter_gray(video_path: Path, fps: float, width: int):
    """Yield (timestamp_sec, grayscale uint8 frame) at `fps`, scaled to `width`."""
    w, h = _probe_wh(video_path)
    out_w = width if width < w else w
    out_h = int(round(h * (out_w / w)))
    out_w -= out_w % 2
    out_h -= out_h % 2
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video_path),
         "-vf", f"fps={fps},scale={out_w}:{out_h}", "-pix_fmt", "gray",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frame_size = out_w * out_h
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            yield idx / fps, np.frombuffer(buf, dtype=np.uint8).reshape((out_h, out_w))
            idx += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait(timeout=5)


def motion_residual(video_path: Path, fps: float = 4.0, width: int = 320):
    """Return (times, residual_px) of camera-compensated subject motion.

    Between consecutive frames: track sparse features, fit the dominant
    (camera) motion with RANSAC, then measure how far the *most-moving* points
    deviate from that model. Background tracks the camera model → ~0; a subject
    moving through frame leaves a large residual. So a shaky pan over a still
    scene scores low while real action scores high.
    """
    import cv2  # lazy: only when motion is actually computed

    feat = dict(maxCorners=200, qualityLevel=0.01, minDistance=8, blockSize=7)
    lk = dict(winSize=(21, 21), maxLevel=3,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))

    times: list[float] = []
    vals: list[float] = []
    prev = None
    for t, gray in _iter_gray(video_path, fps, width):
        if prev is not None:
            residual = 0.0
            p0 = cv2.goodFeaturesToTrack(prev, mask=None, **feat)
            if p0 is not None and len(p0) >= 6:
                p1, status, _ = cv2.calcOpticalFlowPyrLK(prev, gray, p0, None, **lk)
                ok = status.reshape(-1).astype(bool)
                a = p0.reshape(-1, 2)[ok]
                b = p1.reshape(-1, 2)[ok]
                if len(a) >= 6:
                    M, _ = cv2.estimateAffinePartial2D(
                        a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0)
                    if M is not None:
                        pred = a @ M[:, :2].T + M[:, 2]
                        res = np.linalg.norm(b - pred, axis=1)
                    else:  # no dominant motion model — use raw displacement
                        res = np.linalg.norm(b - a, axis=1)
                    # Top quartile: localized subject motion, not diluted by a
                    # mostly-static background.
                    k = max(1, len(res) // 4)
                    residual = float(np.mean(np.sort(res)[-k:]))
            times.append(t)
            vals.append(residual)
        prev = gray
    return np.array(times, dtype=np.float32), np.array(vals, dtype=np.float32)


def _motion01(residual_px: np.ndarray) -> np.ndarray:
    span = MOTION_CEIL - MOTION_FLOOR
    return np.clip((residual_px - MOTION_FLOOR) / span, 0.0, 1.0).astype(np.float32)


# --- combine ---------------------------------------------------------------
def _to_seconds(times: np.ndarray, vals: np.ndarray, n_sec: int) -> np.ndarray:
    """Max-pool an (times, vals) series onto an integer-second grid of len n_sec."""
    g = np.zeros(n_sec, dtype=np.float32)
    for t, v in zip(times, vals):
        i = min(n_sec - 1, int(t))
        if v > g[i]:
            g[i] = v
    return g


def _find_peaks(series: np.ndarray) -> list[float]:
    if series.size == 0:
        return []
    cand = [
        i for i in range(len(series))
        if series[i] >= PEAK_MIN
        and series[i] >= series[max(0, i - 1)]
        and series[i] >= series[min(len(series) - 1, i + 1)]
    ]
    cand.sort(key=lambda i: -series[i])
    chosen: list[int] = []
    for i in cand:
        if all(abs(i - j) >= PEAK_MIN_GAP_SEC for j in chosen):
            chosen.append(i)
        if len(chosen) >= PEAK_MAX:
            break
    return [float(i) for i in sorted(chosen)]


def _aggregate(series: np.ndarray) -> float:
    if series.size == 0:
        return 0.0
    k = max(1, int(round(0.30 * series.size)))
    return float(np.mean(np.sort(series)[-k:]))


def bucket_for(score: float) -> str:
    if score >= BUCKET_HIGH:
        return "high"
    if score <= BUCKET_LOW:
        return "low"
    return "medium"


def score_clip(video_path: Path, *, motion: bool = True) -> EnergyResult:
    """Full per-clip energy: combined 0..1 series, aggregate score, bucket, peaks."""
    video_path = Path(video_path)
    dbfs = audio_dbfs(video_path)
    a01 = _audio01(dbfs) if dbfs.size else np.empty(0, dtype=np.float32)

    m01 = np.empty(0, dtype=np.float32)
    if motion:
        mt, mres = motion_residual(video_path)
        if mt.size:
            n_sec = max(int(np.ceil(float(mt.max()))) + 1, a01.size, 1)
            m01 = _to_seconds(mt, _motion01(mres), n_sec)

    have_audio = a01.size > 0
    have_motion = m01.size > 0

    n = max(a01.size, m01.size, 1)
    audio_g = np.pad(a01, (0, n - a01.size)) if have_audio else np.zeros(n, dtype=np.float32)
    motion_g = np.pad(m01, (0, n - m01.size)) if have_motion else np.zeros(n, dtype=np.float32)

    # soft-OR: 1 - (1-a)(1-m). Either signal alone carries; both boost.
    combined = (1.0 - (1.0 - audio_g) * (1.0 - motion_g)).astype(np.float32)
    score = _aggregate(combined)
    return EnergyResult(
        score=score,
        bucket=bucket_for(score),
        series=combined,
        peaks=_find_peaks(combined),
        have_audio=have_audio,
        have_motion=have_motion,
    )
