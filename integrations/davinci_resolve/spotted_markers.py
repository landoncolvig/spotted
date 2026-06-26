#!/usr/bin/env python3
"""Spotted -> DaVinci Resolve marker import.

Run this INSIDE DaVinci Resolve (Workspace > Scripts, or the Console). It reads
the marker manifest Spotted writes after "Tag & finish"
(~/.facetag/spotted_resolve_markers.json), finds the matching clips in your
current project's Media Pool by file path, and stamps a marker at every moment
a named person appears.

Why this exists: Resolve does not read the Adobe XMP markers Spotted writes for
Premiere, so per-face timeline markers never showed up in DaVinci. Resolve's
scripting API (MediaPoolItem.AddMarker) is the reliable path, but it can only
run inside Resolve, hence this separate script.

Requirements / assumptions (verify in your Resolve version):
- DaVinci Resolve 17 or newer (MediaPoolItem.AddMarker).
- The clips you tagged in Spotted are imported into the currently open project.
- Resolve allows only ONE marker per frame on a clip; when two people land on
  the same sampled frame their names are merged into a single marker.

Install: copy this file into Resolve's Scripts folder, then run it from
Workspace > Scripts. See README.md in this folder for the exact path.
"""
from __future__ import annotations

import json
import os
import sys

MANIFEST = os.path.expanduser("~/.facetag/spotted_resolve_markers.json")
MARKER_COLOR = "Blue"
MARKER_NOTE = "Spotted"


def _get_resolve():
    """Resolve injects `resolve` as a global when a script runs from the
    Scripts menu. Fall back to the scripting module for console/standalone."""
    r = globals().get("resolve")
    if r:
        return r
    candidates = [
        os.environ.get("RESOLVE_SCRIPT_API", "")
        and os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"),
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
        os.path.expanduser(
            "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
        ),
    ]
    for path in filter(None, candidates):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
    try:
        import DaVinciResolveScript as bmd  # type: ignore
        return bmd.scriptapp("Resolve")
    except Exception:
        return None


def _norm(p: str) -> str:
    try:
        return os.path.realpath(p)
    except Exception:
        return p


def _iter_clips(folder):
    """Depth-first walk of every Media Pool clip across all bins."""
    for clip in folder.GetClipList():
        yield clip
    for sub in folder.GetSubFolderList():
        yield from _iter_clips(sub)


def main() -> int:
    if not os.path.exists(MANIFEST):
        print(f"[Spotted] No manifest at {MANIFEST}. Tag a folder in Spotted first.")
        return 1
    try:
        data = json.load(open(MANIFEST))
    except Exception as e:
        print(f"[Spotted] Could not read manifest: {e}")
        return 1

    clips_map = {_norm(k): v for k, v in data.get("clips", {}).items()}
    if not clips_map:
        print("[Spotted] Manifest has no clips with named faces. Name some people in Spotted, then re-run Tag & finish.")
        return 0

    resolve = _get_resolve()
    if not resolve:
        print("[Spotted] Could not connect to DaVinci Resolve. Run this from inside Resolve (Workspace > Scripts).")
        return 1
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        print("[Spotted] No project is open in Resolve.")
        return 1
    root = project.GetMediaPool().GetRootFolder()

    matched = 0
    total_markers = 0
    for clip in _iter_clips(root):
        path = clip.GetClipProperty("File Path")
        if not path:
            continue
        events = clips_map.get(_norm(path))
        if not events:
            continue
        matched += 1

        try:
            fps = float(clip.GetClipProperty("FPS"))
        except (TypeError, ValueError):
            fps = 0.0
        if fps <= 0:
            fps = 24.0

        # Resolve permits one marker per frame, so merge names per frame.
        by_frame: dict[int, list[str]] = {}
        for ev in events:
            frame = int(round(float(ev["t"]) * fps))
            by_frame.setdefault(frame, []).append(ev["name"])

        added = 0
        for frame, names in sorted(by_frame.items()):
            label = ", ".join(dict.fromkeys(names))  # de-dupe, keep order
            if clip.AddMarker(frame, MARKER_COLOR, label, MARKER_NOTE, 1):
                added += 1
        total_markers += added
        print(f"[Spotted] {os.path.basename(path)}: {added} marker(s)")

    print(
        f"[Spotted] Done. Matched {matched} clip(s), stamped {total_markers} marker(s)."
    )
    if matched == 0:
        print(
            "[Spotted] No Media Pool clips matched the manifest. Import the same "
            "files you tagged in Spotted into this project, then run again."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
