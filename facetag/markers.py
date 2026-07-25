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

import json
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


def delete_sidecar_if_spotted(video_path: Path) -> bool:
    """Remove Spotted's own `.xmp` sidecar next to a clip so tagged folders stay
    clean (a user working with thousands of clips doesn't want a sidecar beside
    every one). Only deletes a sidecar Spotted wrote (CreatorTool=Spotted); a
    foreign sidecar — the user's own DaVinci/Bridge color, ratings, or markers —
    is never touched. Returns True if a sidecar was removed."""
    sidecar = sidecar_path_for(video_path)
    if not sidecar.exists():
        return False
    exe = _exiftool()
    if _sidecar_is_spotted(sidecar, exe):
        sidecar.unlink()
        return True
    return False


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


# --- DaVinci Resolve markers ------------------------------------------------
# DaVinci does NOT surface the XMP-xmpDM:Markers that Premiere reads, so markers
# never show up there via metadata. The only reliable path is the Resolve
# scripting API (MediaPoolItem.AddMarker). We emit a small self-contained script
# that the user runs inside Resolve; it matches clips by filename and adds a
# marker on each Media Pool clip at every moment Spotted tagged.

_RESOLVE_TEMPLATE = '''#!/usr/bin/env python
"""Spotted -> DaVinci Resolve markers.

DaVinci doesn't read the in-file XMP markers Spotted writes (Premiere does), so
this adds them through the Resolve API instead — as markers on each Media Pool
clip (the bookmarks on the clip + pins under the source viewer).

HOW TO RUN
  1. QUIT and reopen DaVinci Resolve if it was already running — it only scans
     for scripts at launch, so a freshly-written one won't appear until restart.
  2. Open your project and import the footage.
  3. Workspace menu -> Scripts -> "Spotted Markers".
     (Or Workspace -> Console, click Py3, and run this file.)
Clips are matched by filename. Safe to re-run — Resolve ignores a duplicate
marker on the same frame.
"""
import os

MARKERS = __DATA__


def _get_resolve():
    r = globals().get("resolve")
    if r:
        return r
    try:
        import DaVinciResolveScript as bmd
        return bmd.scriptapp("Resolve")
    except Exception:
        return None


def main():
    resolve = _get_resolve()
    if not resolve:
        print("Spotted: couldn't reach DaVinci Resolve. Run this from "
              "Workspace > Scripts (or the Console) inside Resolve.")
        return
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        print("Spotted: no project is open.")
        return
    media_pool = proj.GetMediaPool()

    def walk(folder):
        clips = list(folder.GetClipList())
        for sub in folder.GetSubFolderList():
            clips += walk(sub)
        return clips

    by_name = {}
    for clip in walk(media_pool.GetRootFolder()):
        path = clip.GetClipProperty("File Path") or ""
        by_name.setdefault(os.path.basename(path) or clip.GetName(), clip)

    total_marks = 0
    matched = 0
    missing = 0
    for name, marks in MARKERS.items():
        clip = by_name.get(name)
        if not clip:
            missing += 1
            continue
        try:
            fps = float(clip.GetClipProperty("FPS") or 0) or 30.0
        except Exception:
            fps = 30.0
        placed = False
        for sec, label, color in marks:
            frame = int(round(float(sec) * fps))
            if clip.AddMarker(frame, color, label, "Spotted", 1):
                total_marks += 1
                placed = True
        matched += 1 if placed else 0
    print("Spotted: added %d markers to %d clips (%d clips in the script "
          "weren't found in this project)." % (total_marks, matched, missing))


main()
'''


def resolve_marker_script(video_markers: dict[str, list[tuple[float, str]]]) -> str:
    """Build the self-contained DaVinci Resolve marker script for a batch.

    `video_markers` maps a clip path -> [(timestamp_sec, label)]. Named-person
    labels get a blue marker, energy peaks yellow. Returns the script text (with
    the marker data embedded), or "" if there's nothing to mark."""
    data: dict[str, list] = {}
    for path, events in video_markers.items():
        rows: list = []
        seen: set[tuple[int, str]] = set()
        for t, label in sorted(events):
            key = (int(round(t * 1000)), label)
            if key in seen:
                continue
            seen.add(key)
            color = "Yellow" if label == "Energy peak" else "Blue"
            rows.append([round(float(t), 3), _sanitize(label), color])
        if rows:
            data[Path(path).name] = rows
    if not data:
        return ""
    return _RESOLVE_TEMPLATE.replace("__DATA__", json.dumps(data))


def write_resolve_script(
    video_markers: dict[str, list[tuple[float, str]]],
    fallback_dir: Path,
) -> Path | None:
    """Write the Resolve marker script into Resolve's user Scripts/Utility
    folder, which appears at the top level of the Workspace > Scripts menu.
    Resolve scans the subfolders (Utility, Comp, Edit, Color, Deliver, Tool),
    NOT the Scripts root, so a script dropped in the root shows up as "No
    Scripts". Fall back to `fallback_dir` (next to the footage) if that write
    fails. Returns where it landed, or None if there was nothing to write.
    NOTE: Resolve scans this folder only at launch, so a just-written script
    needs a Resolve restart to appear."""
    script = resolve_marker_script(video_markers)
    if not script:
        return None
    candidates = [
        Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility",
        fallback_dir,
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            out = d / "Spotted Markers.py"
            out.write_text(script)
            return out
        except Exception:  # noqa: BLE001 - try the next location
            continue
    return None


# --- FCPXML timeline export ---------------------------------------------------
#
# DaVinci Resolve ignores the in-file XMP markers Spotted writes, and the
# Workspace > Scripts route proved unreliable: on a clean Resolve 21 install the
# Scripts menu enumerated nothing at all, from any of the documented locations.
# Importing a timeline is the path that needs no scripting, no preference
# change, and no file hidden inside ~/Library — File > Import > Timeline, and
# the markers are simply there.

# Frame durations Resolve accepts, keyed by nominal fps. NTSC rates must stay
# rational (1001/30000) or markers drift out of sync over a long clip.
_FRAME_DURATIONS: list[tuple[float, int, int]] = [
    (23.976, 1001, 24000),
    (24.0, 1, 24),
    (25.0, 1, 25),
    (29.97, 1001, 30000),
    (30.0, 1, 30),
    (50.0, 1, 50),
    (59.94, 1001, 60000),
    (60.0, 1, 60),
]


def _frame_duration(fps: float) -> tuple[int, int]:
    """Nearest standard frame duration as (numerator, denominator)."""
    best = min(_FRAME_DURATIONS, key=lambda f: abs(f[0] - fps))
    return best[1], best[2]


def _fcp_time(seconds: float, num: int, den: int) -> str:
    """Seconds as an FCPXML rational snapped to the frame grid.

    FCPXML wants times on the timebase; a raw float like "1.234s" is either
    rejected or silently rounded, which is how markers end up on the wrong
    frame.
    """
    frames = int(round(seconds * den / num))
    return f"{frames * num}/{den}s"


def _video_duration(video_path: Path, fps: float) -> float:
    """Clip length in seconds. Falls back to 0 when undetectable."""
    try:
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        try:
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        finally:
            cap.release()
        if frames > 0 and fps > 0:
            return float(frames) / fps
    except Exception:  # noqa: BLE001 - duration is best-effort
        pass
    return 0.0


def _timeline_layout(
    video_markers: dict[str, list[tuple[float, str]]]
) -> tuple[int, int, list[tuple[Path, float, float, list[tuple[float, str]]]]]:
    """Lay the marked clips end to end and return (fd_num, fd_den, entries).

    Both the FCPXML and the EDL are built from this one layout, so a marker's
    timeline position is identical in both files. If they disagreed, the EDL
    markers would land on the wrong clips.
    """
    clips = [(p, ev) for p, ev in sorted(video_markers.items()) if ev]
    if not clips:
        return 1, 30, []
    num, den = _frame_duration(get_video_fps(Path(clips[0][0])))
    entries: list[tuple[Path, float, float, list[tuple[float, str]]]] = []
    offset = 0.0
    for path_str, events in clips:
        p = Path(path_str)
        dur = _video_duration(p, get_video_fps(p))
        if dur <= 0:
            dur = max((t for t, _ in events), default=0.0) + 1.0
        entries.append((p, offset, dur, sorted(events)))
        offset += dur
    return num, den, entries


def _timecode(seconds: float, num: int, den: int) -> str:
    """Seconds as HH:MM:SS:FF at the timeline frame rate."""
    fps = den / num                      # e.g. 30000/1001 -> 29.97
    total = int(round(seconds * fps))
    rate = int(round(fps))               # frame field counts whole frames
    f = total % rate
    total //= rate
    return f"{total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}:{f:02d}"


def edl_for_markers(video_markers: dict[str, list[tuple[float, str]]]) -> str:
    """Build a CMX3600 EDL of timeline markers.

    Resolve imports these with: right-click the timeline in the Media Pool ->
    Timelines > Import > Timeline Markers from EDL. Verified against DaVinci
    Resolve 21 — markers land at the stated timecodes with the stated colors.
    The `|C: |M: |D:` comment line is what carries the marker; the event line
    above it only positions it.
    """
    num, den, entries = _timeline_layout(video_markers)
    if not entries:
        return ""
    lines = ["TITLE: Spotted Markers", "FCM: NON-DROP FRAME", ""]
    n = 0
    for _p, offset, dur, events in entries:
        for t, label in events:
            # Keep the marker inside its own clip; a marker past the last frame
            # is dropped by Resolve without warning.
            at = offset + min(t, max(dur - (num / den), 0.0))
            n += 1
            tc_in = _timecode(at, num, den)
            tc_out = _timecode(at + (num / den), num, den)
            color = "ResolveColorYellow" if label == "Energy peak" else "ResolveColorBlue"
            lines.append(
                f"{n:03d}  001      V     C        {tc_in} {tc_out} {tc_in} {tc_out}"
            )
            lines.append(f" |C:{color} |M:{_edl_safe(label)} |D:1")
            lines.append("")
    return "\n".join(lines)


def _edl_safe(name: str) -> str:
    """Marker names sit in a pipe-delimited comment; strip what would split it."""
    return name.replace("|", "-").replace("\n", " ").strip()


def write_edl(
    video_markers: dict[str, list[tuple[float, str]]], out_dir: Path
) -> Path | None:
    """Write "Spotted Markers.edl" next to the footage."""
    edl = edl_for_markers(video_markers)
    if not edl:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "Spotted Markers.edl"
        out.write_text(edl)
        return out
    except Exception:  # noqa: BLE001 - never fail the marker run over this
        return None


def fcpxml_for_markers(video_markers: dict[str, list[tuple[float, str]]]) -> str:
    """Build an FCPXML timeline whose clips carry Spotted's markers.

    Returns "" when there is nothing to write. Clips are laid end to end on a
    single spine in filename order, each with its own markers, so importing the
    result gives one timeline containing every marked clip.
    """
    clips = [(p, ev) for p, ev in sorted(video_markers.items()) if ev]
    if not clips:
        return ""

    # The sequence format follows the first clip; mixed-rate footage still
    # imports, Resolve just conforms it.
    first_fps = get_video_fps(Path(clips[0][0]))
    num, den = _frame_duration(first_fps)

    resources: list[str] = [
        f'    <format id="r0" name="FFVideoFormat" frameDuration="{num}/{den}s" '
        f'width="1920" height="1080" colorSpace="1-1-1 (Rec. 709)"/>'
    ]
    spine: list[str] = []
    offset = 0.0

    for i, (path_str, events) in enumerate(clips, start=1):
        p = Path(path_str)
        fps = get_video_fps(p)
        dur = _video_duration(p, fps)
        if dur <= 0:
            # Unknown length: make the clip long enough to hold its last marker
            # so nothing gets clipped off the end of the timeline.
            dur = max((t for t, _ in events), default=0.0) + 1.0

        asset_id = f"a{i}"
        name = _xml_escape(p.stem)
        resources.append(
            f'    <asset id="{asset_id}" name="{name}" src="{_file_url(p)}" '
            f'start="0s" duration="{_fcp_time(dur, num, den)}" '
            f'hasVideo="1" hasAudio="1" format="r0"/>'
        )

        markers = "".join(
            f'\n        <marker start="{_fcp_time(min(t, max(dur - (num / den), 0.0)), num, den)}" '
            f'duration="{num}/{den}s" value="{_xml_escape(label)}"/>'
            for t, label in sorted(events)
        )
        spine.append(
            f'      <asset-clip ref="{asset_id}" name="{name}" '
            f'offset="{_fcp_time(offset, num, den)}" start="0s" '
            f'duration="{_fcp_time(dur, num, den)}" format="r0">{markers}\n'
            f'      </asset-clip>'
        )
        offset += dur

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE fcpxml>\n"
        '<fcpxml version="1.8">\n'
        "  <resources>\n" + "\n".join(resources) + "\n  </resources>\n"
        '  <library>\n'
        '    <event name="Spotted">\n'
        '      <project name="Spotted Markers">\n'
        f'        <sequence format="r0" duration="{_fcp_time(offset, num, den)}" '
        'tcStart="0s" tcFormat="NDF">\n'
        "    <spine>\n" + "\n".join(spine) + "\n    </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _file_url(p: Path) -> str:
    from urllib.parse import quote

    return "file://" + quote(str(p.resolve()))


def write_fcpxml(
    video_markers: dict[str, list[tuple[float, str]]], out_dir: Path
) -> Path | None:
    """Write "Spotted Markers.fcpxml" next to the footage. Returns the path, or
    None if there was nothing to write."""
    xml = fcpxml_for_markers(video_markers)
    if not xml:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "Spotted Markers.fcpxml"
        out.write_text(xml)
        return out
    except Exception:  # noqa: BLE001 - never let the export fail the marker run
        return None
