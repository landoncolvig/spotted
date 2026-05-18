"""Build a temporal highlight reel of a named person across one or many videos."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import db


@dataclass
class Clip:
    video_path: Path
    start: float
    end: float


def _group_timestamps(
    times: list[float],
    gap_sec: float = 1.5,
    pad_sec: float = 0.75,
    min_clip_sec: float = 1.0,
) -> list[tuple[float, float]]:
    """Group sorted timestamps into (start, end) ranges. Pads each range, drops too-short clips."""
    if not times:
        return []
    times = sorted(times)
    ranges: list[list[float]] = []
    cur = [times[0], times[0]]
    for t in times[1:]:
        if t - cur[1] <= gap_sec:
            cur[1] = t
        else:
            ranges.append(cur)
            cur = [t, t]
    ranges.append(cur)

    out = []
    for s, e in ranges:
        s2 = max(0.0, s - pad_sec)
        e2 = e + pad_sec
        if e2 - s2 >= min_clip_sec:
            out.append((s2, e2))
    return out


def collect_clips(
    conn,
    name: str,
    video_paths: list[Path] | None = None,
    gap_sec: float = 1.5,
    pad_sec: float = 0.75,
    min_clip_sec: float = 1.0,
) -> list[Clip]:
    rows = db.videos_with_person(conn, name)
    if not rows:
        return []
    selected = {str(p.resolve()) for p in video_paths} if video_paths else None
    clips: list[Clip] = []
    for video_id, path, _hits in rows:
        if selected and str(Path(path).resolve()) not in selected:
            continue
        times = db.face_times_in_video(conn, video_id, name)
        for s, e in _group_timestamps(times, gap_sec, pad_sec, min_clip_sec):
            clips.append(Clip(video_path=Path(path), start=s, end=e))
    return clips


def render_reel(
    clips: list[Clip],
    output: Path,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
    crf: int = 20,
) -> Path:
    """Re-encode each clip to a uniform codec then concat. Robust across mixed source formats."""
    if not clips:
        raise ValueError("no clips to render")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not on PATH")

    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="facetag_reel_"))
    try:
        seg_paths: list[Path] = []
        for i, c in enumerate(clips):
            seg = work / f"seg_{i:05d}.mp4"
            cmd = [
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{c.start:.3f}",
                "-to", f"{c.end:.3f}",
                "-i", str(c.video_path),
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
                "-r", str(fps),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(seg),
            ]
            subprocess.run(cmd, check=True)
            seg_paths.append(seg)

        list_file = work / "concat.txt"
        list_file.write_text("\n".join(f"file '{p}'" for p in seg_paths))

        cmd = [
            "ffmpeg", "-v", "error", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output),
        ]
        subprocess.run(cmd, check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output
