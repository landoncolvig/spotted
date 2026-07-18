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


def videos_with_keywords(
    conn: sqlite3.Connection,
    exclude_tags: set[str] | None = None,
) -> dict[str, list[str]]:
    """Return {video_path: [merged keywords]}.

    Merges two per-clip sources into the editor's single Keywords column:
    1. Named-person tags (from face clustering + labeling) — a name lands only
       on the clips that person actually appears in.
    2. Matched tags from the activity-suggest step — each tag the user typed on
       the welcome screen lands only on the clips where CLIP found it.

    Both are per-clip by design. The user's typed tags are NOT stamped onto
    every clip; they go through the same appears-here-or-not matching as faces,
    so "beach" only tags clips that show a beach. (The raw batch_tags column is
    now just the matcher's input vocabulary, read via db.all_batch_tags().)

    `exclude_tags` drops matched tags the user unchecked on the review screen
    (case-insensitive). It only filters the matched/auto tags — a person name
    that happens to collide with an excluded word is left alone.

    A video is included if either source has at least one entry. The list is
    de-duplicated and sorted for stable XMP output.
    """
    excluded = {t.strip().lower() for t in (exclude_tags or set()) if t.strip()}

    person_rows = conn.execute(
        "SELECT DISTINCT v.path, p.name "
        "FROM faces f "
        "JOIN videos v ON v.id = f.video_id "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != ''"
    ).fetchall()

    auto_rows = conn.execute(
        "SELECT v.path, a.tag FROM auto_tags a JOIN videos v ON v.id = a.video_id"
    ).fetchall()

    # Energy is applied by default to every scored clip (not gated on faces or
    # matched activities), so a clip with nothing but an energy reading still
    # gets its "<bucket> energy" keyword. Honors the same exclude set in case
    # the review screen later lets a user drop it.
    energy_rows = conn.execute(
        "SELECT path, energy_bucket FROM videos "
        "WHERE energy_bucket IS NOT NULL AND energy_bucket != ''"
    ).fetchall()

    merged: dict[str, set[str]] = {}
    for path, name in person_rows:
        merged.setdefault(path, set()).add(name)
    for path, tag in auto_rows:
        if tag.strip().lower() in excluded:
            continue
        merged.setdefault(path, set()).add(tag)
    for path, bucket in energy_rows:
        kw = f"{bucket} energy"
        if kw.lower() in excluded:
            continue
        merged.setdefault(path, set()).add(kw)

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

    Issued as TWO exiftool calls (XMP, then Keys) instead of one combined
    invocation. Combining `-Tag=` (clear) and `-Tag+=X` (add) on the same
    tag in a single command silently ignores the clear — exiftool processes
    the operations as a sequence, but treats the clear as superseded by the
    subsequent add, so XMP-dc:Subject duplicates on every run. Splitting
    also surfaces per-namespace failures cleanly (e.g. a sidecar with a
    busted Keys-atom write doesn't silently swallow Keys errors behind an
    XMP success).
    """
    if not names:
        return
    exe = _exiftool_path()

    # --- 1) XMP-dc:Subject (Premiere) ---
    # `-sep ", "` lets us write the entire bag in a single -XMP-dc:Subject=…
    # assignment. exiftool splits the value into bag elements on the sep
    # string. Without -sep, the only way to get N elements is N separate
    # `-XMP-dc:Subject+=` adds, but mixing `=` and `+=` on the same tag in
    # one command treats the `=` as an add (not a clear) — so re-running
    # the write duplicates entries every time. -sep avoids that footgun.
    xmp_args: list[str] = [exe, "-overwrite_original"]
    if replace:
        xmp_args += ["-sep", ", ", f"-XMP-dc:Subject={', '.join(names)}"]
    else:
        for name in names:
            xmp_args.append(f"-XMP-dc:Subject+={name}")
    xmp_args.append(str(video_path))
    r1 = subprocess.run(xmp_args, capture_output=True, text=True)
    if r1.returncode != 0:
        raise RuntimeError(
            f"exiftool XMP write failed on {video_path.name}: "
            f"{r1.stderr.strip() or r1.stdout.strip()}"
        )

    # --- 2) Keys:Keywords (DaVinci, FCP, Apple Photos) ---
    # Single comma-joined string, always replaces. -api QuickTimeHandler=1
    # is required for exiftool to write into the com.apple.quicktime.keywords
    # atom (without it, the write silently no-ops on .mov / .mp4).
    keys_args = [
        exe, "-overwrite_original",
        "-api", "QuickTimeHandler=1",
        f"-Keys:Keywords={', '.join(names)}",
        str(video_path),
    ]
    r2 = subprocess.run(keys_args, capture_output=True, text=True)
    if r2.returncode != 0:
        raise RuntimeError(
            f"exiftool Keys write failed on {video_path.name}: "
            f"{r2.stderr.strip() or r2.stdout.strip()}"
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
