"""Frame extraction via ffmpeg subprocess.

Yields (timestamp_sec, BGR frame ndarray) at a configurable sample rate.
Resizes to a max dimension to keep detection fast — original frame is not needed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import numpy as np

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".mpg", ".mpeg"}


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def walk_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if is_video(root) else []
    return sorted(p for p in root.rglob("*") if is_video(p))


def probe(video_path: Path) -> tuple[float, int, int]:
    """Return (duration_sec, width, height). Raises if ffprobe fails."""
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not on PATH; install ffmpeg via brew")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    data = json.loads(out)
    stream = data["streams"][0]
    duration = float(data["format"]["duration"])
    return duration, int(stream["width"]), int(stream["height"])


def _noop_marker():  # pragma: no cover - placeholder so the attr always exists
    pass


def iter_frames(
    video_path: Path,
    sample_fps: float = 1.0,
    max_side: int = 960,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (timestamp_sec, BGR uint8 ndarray) sampled at sample_fps.

    Frames are resized so the longest side is at most max_side. Aspect preserved.
    """
    duration, w, h = probe(video_path)
    scale = max_side / max(w, h)
    out_w = int(round(w * scale)) if scale < 1 else w
    out_h = int(round(h * scale)) if scale < 1 else h
    out_w -= out_w % 2  # ffmpeg rgb24 wants even-ish but rgb24 is fine
    out_h -= out_h % 2

    cmd = [
        "ffmpeg", "-v", "error",
        "-i", str(video_path),
        "-vf", f"fps={sample_fps},scale={out_w}:{out_h}",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    frame_size = out_w * out_h * 3
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape((out_h, out_w, 3))
            t = idx / sample_fps
            yield t, frame
            idx += 1
            if t > duration + 1:
                break
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait(timeout=5)
        # A decode that dies partway used to be invisible: stderr was discarded,
        # the return code never checked, and the loop simply broke on a short
        # read. The clip was then marked fully scanned with half its faces
        # missing and nothing anywhere said so.
        err = b""
        if proc.stderr:
            try:
                err = proc.stderr.read() or b""
            except Exception:  # noqa: BLE001
                err = b""
            proc.stderr.close()
        expected = int(duration * sample_fps) if duration else 0
        iter_frames.last_result = {
            "returncode": proc.returncode,
            "frames": idx,
            "expected": expected,
            "stderr": err.decode("utf-8", "replace").strip()[:400],
            # Losing a couple of frames at the tail is normal; losing a third of
            # the clip is a failed decode.
            "short": bool(expected and idx < expected * 0.66),
        }
