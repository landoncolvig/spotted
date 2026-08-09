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

# Everything ffmpeg can read, which is everything Spotted can scan, mark and
# put on a DaVinci timeline. AVCHD (.mts/.m2ts) is what Sony and Panasonic
# camcorders write; without it a folder of them reports "no videos found",
# which reads as the app being broken rather than the format being unlisted.
VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".flv", ".wmv",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".mxf", ".ts", ".3gp", ".mqv",
}

# Camera raw formats whose picture only a vendor SDK can decode. ffmpeg can
# demux an .r3d but has no REDCODE decoder, so no frame ever comes out, and
# there is nothing to detect a face in. Listing them here is not support: it
# is so a folder of them gets an answer that explains itself and names the
# way through, instead of a bare "no videos found" that reads as a bug.
CAMERA_RAW_EXTS = {".r3d", ".braw", ".ari", ".arri", ".crm", ".cine"}


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def camera_raw_in(paths: list[Path]) -> list[str]:
    """Raw formats found under these paths, as sorted extensions.

    Used to turn "no videos found" into something actionable when someone
    points Spotted at a folder of RED or Blackmagic originals.
    """
    found: set[str] = set()
    for root in paths:
        candidates = [root] if root.is_file() else root.rglob("*")
        for p in candidates:
            suffix = p.suffix.lower()
            if suffix in CAMERA_RAW_EXTS:
                found.add(suffix)
    return sorted(found)


def not_downloaded(videos: list[Path]) -> list[Path]:
    """Clips that exist in the index sense but have no data on this disk.

    iCloud Drive, Dropbox and OneDrive all evict file contents while leaving
    something that looks like a file. Spotted used to discover this one clip at
    a time, deep inside a scan, as a decode failure per clip — which reads as
    "this footage is broken" rather than "these are not downloaded yet".

    Two signals, because the providers differ:

    `st_blocks == 0` with a non-zero size means the file occupies no space, so
    whatever the directory entry says, the bytes are elsewhere. This is what an
    APFS dataless placeholder looks like.

    A sibling `.<name>.icloud` is what iCloud leaves when it evicts a file
    outright: the real name disappears and a small plist takes its place. It is
    checked separately because in that case the video path does not exist at
    all, so there is nothing to stat.
    """
    missing: list[Path] = []
    for v in videos:
        try:
            st = v.stat()
        except OSError:
            # Gone entirely, or the placeholder form. Either way there is
            # nothing here to read.
            if (v.parent / f".{v.name}.icloud").exists():
                missing.append(v)
            continue
        if st.st_size > 0 and st.st_blocks == 0:
            missing.append(v)
    return missing


def walk_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if is_video(root) else []
    return sorted(p for p in root.rglob("*") if is_video(p))


def probe(video_path: Path) -> tuple[float, int, int]:
    """Return (duration_sec, width, height) as the frame will actually decode.

    Phones record portrait video as a LANDSCAPE stream plus a 90 degree
    rotation flag. ffmpeg applies that flag on decode, so the frame that comes
    out is portrait while `stream=width,height` still reports landscape.
    Reporting the raw numbers made iter_frames scale a 1080x1920 frame into a
    960x540 box, squashing every face to a third of its proper width. Face
    detection on rotated phone footage degraded badly and looked like a model
    problem rather than a geometry one.
    """
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not on PATH; install ffmpeg via brew")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height:stream_side_data=rotation:stream_tags=rotate"
        ":format=duration",
        "-of", "json",
        str(video_path),
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    data = json.loads(out)
    stream = data["streams"][0]
    duration = float(data["format"]["duration"])
    w, h = int(stream["width"]), int(stream["height"])
    if _rotation_swaps_axes(stream):
        w, h = h, w
    return duration, w, h


def _rotation_swaps_axes(stream: dict) -> bool:
    """Does this stream's rotation flag turn landscape into portrait?

    The angle lives in a Display Matrix side_data on modern files and in a
    `rotate` tag on older ones, so check both. Only quarter turns swap the
    axes; 180 leaves them alone.
    """
    angles = [sd.get("rotation") for sd in stream.get("side_data_list", [])]
    angles.append(stream.get("tags", {}).get("rotate"))
    for angle in angles:
        if angle is None:
            continue
        try:
            if abs(int(float(angle))) % 180 == 90:
                return True
        except (TypeError, ValueError):
            continue
    return False


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
