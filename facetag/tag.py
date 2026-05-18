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

    Only includes videos that contain at least one *named* person — videos
    where every face is in an unnamed cluster get nothing written.
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


def write_keywords(video_path: Path, names: list[str], *, replace: bool = True) -> None:
    """Write keywords into a video file via exiftool.

    `replace=True` (default) clears existing keywords first so the set is
    exactly the named people. `replace=False` appends to whatever is there.
    """
    if not names:
        return
    exe = _exiftool_path()
    args = [exe, "-overwrite_original", "-q"]
    if replace:
        args.append("-Keywords=")  # clear
    for name in names:
        args.append(f"-Keywords+={name}")
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
        [exe, "-s", "-s", "-s", "-Keywords", str(video_path)],
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
