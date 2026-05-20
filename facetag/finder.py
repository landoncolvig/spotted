"""Write tags into macOS metadata so Finder finds and displays them.

Three metadata channels per file, each with a different purpose:

- **kMDItemFinderComment** (Spotlight Comment, single string).
  Indexed by Spotlight and shown in Get Info → Comments. Searchable as
  a phrase ("Fat lady" finds files containing that exact substring).
  XMP-dc:Subject is NOT indexed by Spotlight on .mov files, so the
  comment is the load-bearing search field.

- **_kMDItemUserTags** (Finder Tags, multi-value list).
  Whitespace-preserving — "Fat lady" stays as one chip in Finder
  Get Info → Tags and appears in the Finder sidebar tag list. The
  QuickTime Keys:Keywords atom (which Spotted also writes for DaVinci)
  is auto-tokenized by Spotlight into per-word entries in kMDItemKeywords,
  which is why "Fat lady" shows up as "Fat" + "lady" in the Keywords
  row of Get Info. Finder Tags side-step that tokenization so the user
  sees the labels they actually typed.

- **mdimport** is run after writing both so Spotlight picks up the
  change immediately instead of waiting for its periodic re-scan.
"""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path


XATTR_FINDER_COMMENT = "com.apple.metadata:kMDItemFinderComment"
XATTR_USER_TAGS = "com.apple.metadata:_kMDItemUserTags"

# Backwards-compat alias — `tag.py` and tests import this name.
XATTR_KEY = XATTR_FINDER_COMMENT


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _write_xattr(video_path: Path, key: str, plist_value) -> None:
    """Serialize `plist_value` as a binary plist xattr."""
    data = plistlib.dumps(plist_value, fmt=plistlib.FMT_BINARY)
    result = subprocess.run(
        ["xattr", "-wx", key, data.hex(), str(video_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"xattr {key} failed on {video_path.name}: {result.stderr.strip()}"
        )


def _read_xattr(video_path: Path, key: str):
    r = subprocess.run(
        ["xattr", "-px", key, str(video_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return plistlib.loads(bytes.fromhex("".join(r.stdout.split())))
    except Exception:
        return None


def write_finder_comment(video_path: Path, keywords: list[str]) -> None:
    """Write the Spotlight Comment AND Finder Tags, then nudge mdimport.

    Both writes survive the file copy/move that Final Cut and DaVinci
    sometimes perform on import — xattrs travel with the file across
    APFS volumes. Re-running with the same keywords is a no-op (xattr
    overwrites with the same bytes).
    """
    if not keywords:
        return

    # 1) Spotlight Comment — single comma-joined string.
    _write_xattr(video_path, XATTR_FINDER_COMMENT, ", ".join(keywords))

    # 2) Finder Tags — one entry per keyword. The plist value is a list
    # of "<TagName>\n<ColorIndex>" strings; we use color 0 (no color) so
    # the tags appear in Finder without polluting the colored-tag UI.
    # Each multi-word name stays whole because Finder Tags are an array
    # of strings, not a tokenized field.
    tag_entries = [f"{kw}\n0" for kw in keywords]
    _write_xattr(video_path, XATTR_USER_TAGS, tag_entries)

    # Nudge Spotlight to pick up both xattrs immediately.
    if _have("mdimport"):
        subprocess.run(
            ["mdimport", str(video_path)],
            capture_output=True,
            timeout=10,
        )


def read_finder_comment(video_path: Path) -> str | None:
    """Read back the Spotlight Comment. Returns None if not set."""
    val = _read_xattr(video_path, XATTR_FINDER_COMMENT)
    return val if isinstance(val, str) else None


def read_finder_tags(video_path: Path) -> list[str]:
    """Read back Finder Tag names (strips the trailing color index)."""
    val = _read_xattr(video_path, XATTR_USER_TAGS)
    if not isinstance(val, list):
        return []
    out: list[str] = []
    for entry in val:
        if isinstance(entry, str):
            # Format is "name\n<colorIndex>"; the color is optional.
            out.append(entry.split("\n", 1)[0])
    return out
