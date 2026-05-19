"""Write tags into macOS Spotlight Comments so Finder search finds clips by name.

macOS Finder indexes a handful of fields via Spotlight: filename, file
content (for known types), and the per-file "Spotlight Comment"
(kMDItemFinderComment). It does NOT index XMP-dc:Subject for .mov files —
that's an Adobe-specific field that only Premiere/DaVinci know to read.

So Spotted writes the same keywords twice:
- XMP-dc:Subject  → Premiere & DaVinci read this as Keywords
- kMDItemFinderComment → Finder search and Get Info → Comments read this

The xattr key is `com.apple.metadata:kMDItemFinderComment` with a binary
plist value containing a single string. After writing we run `mdimport`
on the file to force Spotlight to pick up the change immediately
instead of waiting for its periodic re-scan.
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path


XATTR_KEY = "com.apple.metadata:kMDItemFinderComment"


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def write_finder_comment(video_path: Path, keywords: list[str]) -> None:
    """Set the Spotlight Comment to a comma-joined keyword list and re-index.

    Comma-joined matches Premiere's Keywords display format and is what
    Spotlight tokenizes for search ("Sarah, baptism, kids" finds files
    on a search for any of those words).

    Idempotent: re-running with the same keywords produces the same xattr.
    """
    if not keywords:
        return

    comment = ", ".join(keywords)
    data = plistlib.dumps(comment, fmt=plistlib.FMT_BINARY)

    # xattr -wx accepts a hex string and writes it as raw bytes
    result = subprocess.run(
        ["xattr", "-wx", XATTR_KEY, data.hex(), str(video_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"xattr failed on {video_path.name}: {result.stderr.strip()}"
        )

    # Nudge Spotlight to pick up the change immediately
    if _have("mdimport"):
        subprocess.run(
            ["mdimport", str(video_path)],
            capture_output=True,
            timeout=10,
        )


def read_finder_comment(video_path: Path) -> str | None:
    """Read the Spotlight Comment back for verification."""
    r = subprocess.run(
        ["xattr", "-px", XATTR_KEY, str(video_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        raw_hex = "".join(r.stdout.split())
        data = bytes.fromhex(raw_hex)
        return plistlib.loads(data)
    except Exception:
        return None
