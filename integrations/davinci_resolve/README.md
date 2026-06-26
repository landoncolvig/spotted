# Spotted markers in DaVinci Resolve

Spotted writes per-face timeline markers as Adobe XMP, which Premiere reads but
DaVinci Resolve ignores. This script stamps the same markers into Resolve using
its scripting API, so a marker shows on the scrubber every time a named person
appears in a clip.

Keywords already work in Resolve (you can search the Media Pool by name without
this). This is only for the clickable timeline markers.

## How it works

When you hit "Tag & finish" in Spotted, it writes a manifest to:

```
~/.facetag/spotted_resolve_markers.json
```

`spotted_markers.py` reads that manifest, matches it against the clips in your
open Resolve project by file path, and adds one marker per face appearance.

## One-time install

Copy `spotted_markers.py` into Resolve's Scripts folder:

```
mkdir -p "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
cp spotted_markers.py "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/"
```

Resolve also needs external scripting allowed: Resolve > Preferences > System >
General > "External scripting using" set to Local (or Network).

## Each time

1. Tag your footage in Spotted (faces named, "Tag & finish").
2. In Resolve, open the project and import the same clips into the Media Pool.
3. Workspace > Scripts > spotted_markers.
4. Markers appear on each clip. Open a clip in the source viewer or drop it on a
   timeline to see them on the scrubber.

## Requirements and limits

- DaVinci Resolve 17 or newer (uses `MediaPoolItem.AddMarker`).
- The clips must be imported into the open project; the script matches by file
  path, so the files Resolve sees must be the same ones Spotted tagged.
- Resolve allows one marker per frame on a clip. When two people land on the
  same sampled frame, the script merges their names into a single marker.
- Markers are added to Media Pool (source) clips, so they travel with the clip
  into any timeline you cut.

## Not yet verified

This path has not been confirmed against a live Resolve install yet. If markers
don't appear, the likely culprits are the Resolve version, the external-scripting
preference, or a file-path mismatch between the manifest and the Media Pool. Run
the script from Workspace > Console to see its per-clip log.
