"""Write per-face timeline markers into video XMP metadata.

Premiere Pro and DaVinci Resolve read the Adobe XMP Dynamic Media `Tracks`
schema as clip markers on the timeline scrubber. This module writes one
marker per named-face detection: "Sarah at 0:03, Dad at 0:08, Ellie at 0:15".

Editors can then click the marker icons in the timeline to jump to the
exact frame a person appears.

This is verified end-to-end if exiftool reports a non-empty Track field
after writing. Whether Premiere/DaVinci surface the markers depends on
their version and the specific XMP struct format they accept — first
shipping needs human verification in the actual editor.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path


class ExiftoolMissing(RuntimeError):
    pass


def _exiftool() -> str:
    p = shutil.which("exiftool")
    if not p:
        raise ExiftoolMissing("exiftool not found on PATH.")
    return p


def get_video_fps(video_path: Path) -> float:
    """Pull frame rate from the file. Falls back to 29.97 if undetectable."""
    try:
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        finally:
            cap.release()
        if fps > 0:
            return float(fps)
    except Exception:
        pass
    return 29.97


def face_events_for_video(
    conn: sqlite3.Connection, video_id: int
) -> list[tuple[float, str]]:
    """Return [(timestamp_sec, name)] for every named face in the video."""
    rows = conn.execute(
        "SELECT f.timestamp_sec, p.name "
        "FROM faces f "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE f.video_id = ? AND p.name IS NOT NULL AND p.name != '' "
        "ORDER BY f.timestamp_sec",
        (video_id,),
    ).fetchall()
    return [(float(t), n) for t, n in rows]


def videos_with_named_faces(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return [(video_id, video_path)] for videos that have at least one named face."""
    rows = conn.execute(
        "SELECT DISTINCT v.id, v.path "
        "FROM faces f "
        "JOIN videos v ON v.id = f.video_id "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != ''"
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def _sanitize(name: str) -> str:
    """Marker names go into exiftool's struct syntax. Strip characters that
    would break the parser (commas, equals, braces, brackets)."""
    return (
        name.replace(",", " ")
        .replace("=", " ")
        .replace("{", "(")
        .replace("}", ")")
        .replace("[", "(")
        .replace("]", ")")
        .strip()
    )


def write_markers(video_path: Path, face_events: list[tuple[float, str]]) -> None:
    """Write per-face markers to video_path's XMP-xmpDM:Markers.

    Each face appearance becomes a Marker on the clip's timeline. Premiere
    Pro and DaVinci Resolve render these as clickable marker icons on the
    timeline scrubber when the clip is loaded.

    Idempotent: clears existing Markers (-=) before writing the new set,
    so re-running doesn't duplicate.

    StartTime is written in seconds (e.g. "10.5s") which is what Premiere
    expects. Duration is left short (0.5s) so markers display as point
    markers rather than ranges.

    Raises RuntimeError if exiftool fails.
    """
    if not face_events:
        return
    exe = _exiftool()

    args: list[str] = [exe, "-overwrite_original", "-q", "-XMP-xmpDM:Markers-="]
    # De-dupe (timestamp, name) collisions
    seen: set[tuple[int, str]] = set()
    for t, name in sorted(face_events):
        key = (int(round(t * 1000)), name)
        if key in seen:
            continue
        seen.add(key)
        safe = _sanitize(name)
        # 's' suffix tells exiftool/Premiere this is seconds (not samples)
        args.append(
            f"-XMP-xmpDM:Markers+={{Name={safe},StartTime={t:.3f}s,Duration=0.5s,Type=Cue}}"
        )
    args.append(str(video_path))

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"exiftool markers failed on {video_path.name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def read_markers(video_path: Path) -> str:
    """Return the raw Markers value exiftool reads back — for verification."""
    exe = _exiftool()
    r = subprocess.run(
        [exe, "-s", "-s", "-s", "-struct", "-XMP-xmpDM:Markers", str(video_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip()
