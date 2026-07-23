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

    Idempotent: clears existing Markers before writing the new set, so
    re-running doesn't duplicate. The clear is a SEPARATE exiftool call —
    combining a clear and `+=` on the same list tag in one invocation silently
    drops the clear (the same footgun documented in tag.py), and `-Markers-=`
    with no value clears nothing, so the old code duplicated every marker on
    each run.

    StartTime is written in seconds (e.g. "10.5s") which is what Premiere
    expects. Duration is left short (0.5s) so markers display as point
    markers rather than ranges.

    Raises RuntimeError if exiftool fails.
    """
    if not face_events:
        return
    exe = _exiftool()

    clear = subprocess.run(
        [exe, "-overwrite_original", "-q", "-XMP-xmpDM:Markers=", str(video_path)],
        capture_output=True, text=True,
    )
    if clear.returncode != 0:
        raise RuntimeError(
            f"exiftool marker clear failed on {video_path.name}: "
            f"{clear.stderr.strip() or clear.stdout.strip()}"
        )

    args: list[str] = [exe, "-overwrite_original", "-q"]
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


def sidecar_path_for(video_path: Path) -> Path:
    """Return the XMP sidecar path Adobe Bridge/DaVinci look for, e.g.
    `clip.mov` → `clip.mov.xmp`. Appended-extension form (vs replacing the
    extension) is the Adobe convention for video/audio sidecars."""
    return video_path.with_name(f"{video_path.name}.xmp")


def _sidecar_is_spotted(sidecar: Path, exe: str) -> bool:
    """True only if Spotted wrote this .xmp (CreatorTool=Spotted), so it's ours
    to replace. False for any foreign sidecar (DaVinci/Bridge color, ratings,
    the user's own markers) — those must never be destroyed."""
    r = subprocess.run(
        [exe, "-s", "-s", "-s", "-XMP-xmp:CreatorTool", str(sidecar)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0 and r.stdout.strip() == "Spotted"


def write_markers_sidecar(video_path: Path, face_events: list[tuple[float, str]]) -> Path | None:
    """Write a `clipname.mov.xmp` sidecar alongside the clip with markers.

    DaVinci Resolve's in-file XMP marker reading is inconsistent across
    versions (works for some, silently ignored by others). Sidecar XMPs
    are read more reliably via DaVinci's "Sidecar Files" project option.
    This is additive: write_markers still writes in-file XMP for Premiere
    and any DaVinci version that does read it.

    Returns the sidecar path on success, or None if there's nothing to write
    OR a foreign sidecar was preserved. Idempotent for Spotted's own sidecars
    (replaces them); it will NEVER overwrite a sidecar another tool created.
    """
    if not face_events:
        return None
    exe = _exiftool()
    sidecar = sidecar_path_for(video_path)

    # A sidecar may already exist. If Spotted wrote it, replace it so re-tagging
    # refreshes the markers. If another tool wrote it, that file may hold the
    # user's color/ratings/markers — never destroy it. Skip the sidecar write;
    # in-file XMP markers (write_markers) still cover Premiere and any DaVinci
    # that reads them. exiftool's -o refuses to overwrite, so ours is unlinked.
    if sidecar.exists():
        if not _sidecar_is_spotted(sidecar, exe):
            return None
        sidecar.unlink()

    args: list[str] = [exe, "-q", "-o", str(sidecar), "-XMP-xmp:CreatorTool=Spotted"]
    seen: set[tuple[int, str]] = set()
    for t, name in sorted(face_events):
        key = (int(round(t * 1000)), name)
        if key in seen:
            continue
        seen.add(key)
        safe = _sanitize(name)
        args.append(
            f"-XMP-xmpDM:Markers+={{Name={safe},StartTime={t:.3f}s,Duration=0.5s,Type=Cue}}"
        )

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"exiftool sidecar markers failed on {sidecar.name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return sidecar


def read_markers_sidecar(video_path: Path) -> str:
    """Return the raw Markers value from the sidecar XMP, for verification."""
    sidecar = sidecar_path_for(video_path)
    if not sidecar.exists():
        return ""
    exe = _exiftool()
    r = subprocess.run(
        [exe, "-s", "-s", "-s", "-struct", "-XMP-xmpDM:Markers", str(sidecar)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip()
