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

    Writes to two keyword namespaces so both editor families see the data:

    1. **XMP-dc:Subject** — Adobe XMP. Premiere Pro reads this directly
       as Keywords in the Project panel.
    2. **Keys:Keywords** — QuickTime `com.apple.quicktime.keywords` atom.
       DaVinci Resolve, Final Cut Pro, and Apple Photos read this as the
       Keywords field. The `-api QuickTimeHandler=1` flag is required for
       exiftool to write into the Keys atom.

    The IPTC:Keywords alias DOES NOT WORK on .mov files (silently no-ops);
    that's why we write to the underlying namespaces directly.

    `replace=True` (default) clears each namespace first so the set is
    exactly the new keywords. `replace=False` appends.
    """
    if not names:
        return
    exe = _exiftool_path()
    args = [
        exe, "-overwrite_original", "-q",
        "-api", "QuickTimeHandler=1",
    ]
    if replace:
        # XMP-dc:Subject is a multi-value Bag; clear before per-name +=
        args.append("-XMP-dc:Subject=")
    for name in names:
        args.append(f"-XMP-dc:Subject+={name}")
    # Keys:Keywords is a single string (not a multi-value list under exiftool),
    # so we set the whole comma-joined value once. Always replaces; behavior
    # matches XMP since names came from one canonical mapping.
    args.append(f"-Keys:Keywords={', '.join(names)}")
    args.append(str(video_path))

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"exiftool failed on {video_path.name}: {result.stderr.strip() or result.stdout.strip()}"
        )


def read_keywords(video_path: Path) -> dict[str, list[str]]:
    """Read back keywords from each namespace. Useful for verification."""
    exe = _exiftool_path()
    out: dict[str, list[str]] = {}
    for ns, tag in [("xmp", "-XMP-dc:Subject"), ("keys", "-Keys:Keywords")]:
        result = subprocess.run(
            [exe, "-s", "-s", "-s", "-sep", ", ", tag, str(video_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            out[ns] = [k.strip() for k in raw.split(",") if k.strip()] if raw else []
        else:
            out[ns] = []
    return out
