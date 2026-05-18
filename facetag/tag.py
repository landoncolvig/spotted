"""Write per-video person keywords into video file metadata via exiftool.

Premiere reads XMP-dc:Subject as the Keywords column in the Project panel.
DaVinci Resolve surfaces the same field in the Media Pool. Writing both
IPTC:Keywords and XMP-dc:Subject (which the exiftool `Keywords` alias does
in one shot) covers both apps plus everything that reads either standard.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path


class ExiftoolMissing(RuntimeError):
    pass


def _exiftool_path() -> str:
    path = shutil.which("exiftool")
    if not path:
        raise ExiftoolMissing(
            "exiftool not found on PATH. Install with `brew install exiftool`."
        )
    return path


def videos_with_names(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {video_path: [unique sorted names appearing in it]}.

    Only includes videos that contain at least one *named* person.
    """
    rows = conn.execute(
        "SELECT DISTINCT v.path, p.name "
        "FROM faces f "
        "JOIN videos v ON v.id = f.video_id "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != '' "
        "ORDER BY v.path, p.name"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for path, name in rows:
        out.setdefault(path, []).append(name)
    return out


def videos_with_keywords(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {video_path: [merged keywords]}.

    Merges named-person tags AND batch-level tags. A video is included if it
    has at least one of either (named cluster OR non-empty batch_tags). The
    keyword list is de-duplicated and sorted for stable XMP output.
    """
    # Persons appearing in each video.
    person_rows = conn.execute(
        "SELECT DISTINCT v.path, p.name "
        "FROM faces f "
        "JOIN videos v ON v.id = f.video_id "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != ''"
    ).fetchall()

    # Batch tags per video (independent of whether faces were named).
    tag_rows = conn.execute(
        "SELECT path, batch_tags FROM videos WHERE batch_tags IS NOT NULL AND batch_tags != ''"
    ).fetchall()

    merged: dict[str, set[str]] = {}
    for path, name in person_rows:
        merged.setdefault(path, set()).add(name)
    for path, tags_csv in tag_rows:
        for t in tags_csv.split(","):
            t = t.strip()
            if t:
                merged.setdefault(path, set()).add(t)

    return {p: sorted(s) for p, s in merged.items()}


def write_keywords(video_path: Path, names: list[str], *, replace: bool = True) -> None:
    """Write keywords into a video file via exiftool.

    Uses `XMP-dc:Subject` (the multi-value Bag field) rather than the
    `-Keywords` alias, because the alias maps to IPTC:Keywords which
    QuickTime .mov files don't support as a writable container — the
    alias silently no-ops on .mov. Premiere Pro and DaVinci Resolve both
    surface XMP-dc:Subject as the Keywords column.

    `replace=True` (default) clears Subject first so the set is exactly
    the new keywords. `replace=False` appends.
    """
    if not names:
        return
    exe = _exiftool_path()
    args = [exe, "-overwrite_original", "-q"]
    if replace:
        # Empty assignment clears existing values
        args.append("-XMP-dc:Subject=")
    for name in names:
        args.append(f"-XMP-dc:Subject+={name}")
    args.append(str(video_path))

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"exiftool failed on {video_path.name}: {result.stderr.strip() or result.stdout.strip()}"
        )


def read_keywords(video_path: Path) -> list[str]:
    """Read back keywords from a file (for verification)."""
    exe = _exiftool_path()
    result = subprocess.run(
        [exe, "-s", "-s", "-s", "-sep", ", ", "-XMP-dc:Subject", str(video_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    raw = result.stdout.strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]
