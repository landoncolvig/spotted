#!/bin/bash
# Tells Spotted exactly what REDline on THIS machine can do.
# Usage: bash redline_probe.sh /path/to/one/clip.R3D
OUT="$HOME/Desktop/spotted-redline-report.txt"
exec > "$OUT" 2>&1
echo "=== where is REDline ==="
which REDline
find /Applications -maxdepth 4 -iname "REDline*" -o -maxdepth 4 -iname "REDCINE*" 2>/dev/null | head -20
RL="$(which REDline)"
[ -z "$RL" ] && RL="$(find /Applications -maxdepth 4 -iname REDline -type f 2>/dev/null | head -1)"
echo "chosen: $RL"
[ -z "$RL" ] && { echo "NOT FOUND"; exit 0; }
echo; echo "=== help (what this version actually supports) ==="
"$RL" --help 2>&1 | head -80
CLIP="$1"
[ -z "$CLIP" ] && { echo; echo "no clip given, skipping metadata test"; exit 0; }
echo; echo "=== metadata for $CLIP ==="
"$RL" --i "$CLIP" --printMeta 1 2>&1 | head -40
echo; echo "=== try exporting ONE frame at 1/8 res ==="
mkdir -p /tmp/spotted-redline-test
"$RL" --i "$CLIP" --outDir /tmp/spotted-redline-test --o probe --format 1 --res 8 --start 0 --end 0 2>&1 | tail -20
echo "--- files produced ---"
ls -la /tmp/spotted-redline-test 2>/dev/null | head
