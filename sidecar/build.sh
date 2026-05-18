#!/usr/bin/env bash
# Build the Spotted sidecar binary for the current macOS arch.
# Output ends up at app/src-tauri/binaries/spotted-sidecar-<target-triple>
#
# Prereqs (one-time):
#   brew install exiftool
#   cd .. && python3.13 -m venv .venv && .venv/bin/pip install -e . pyinstaller
#
# Usage:
#   ./build.sh
set -euo pipefail

SIDECAR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SIDECAR_DIR/.." && pwd)"
VENV="$REPO/.venv"
PYTHON="$VENV/bin/python3"
PYINSTALLER="$VENV/bin/pyinstaller"

if [[ ! -x "$PYINSTALLER" ]]; then
  echo "PyInstaller not found in venv: $PYINSTALLER"
  echo "Run: $VENV/bin/pip install pyinstaller"
  exit 1
fi

# 1) Ensure the buffalo_l model is on disk under ~/.insightface
if [[ ! -d "$HOME/.insightface/models/buffalo_l" ]]; then
  echo "Pre-fetching buffalo_l InsightFace model..."
  "$PYTHON" - <<'PY'
import insightface
app = insightface.app.FaceAnalysis(name="buffalo_l")
app.prepare(ctx_id=-1)  # CPU init triggers download
print("Model downloaded.")
PY
fi

# 2) Stage exiftool into sidecar/vendor/exiftool so the spec can pick it up.
EXIFTOOL_BIN="$(command -v exiftool || true)"
if [[ -z "$EXIFTOOL_BIN" ]]; then
  echo "exiftool not on PATH. Run: brew install exiftool"
  exit 1
fi
EXIFTOOL_REAL="$(readlink -f "$EXIFTOOL_BIN" 2>/dev/null || python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$EXIFTOOL_BIN")"
EXIFTOOL_PREFIX="$(dirname "$(dirname "$EXIFTOOL_REAL")")"

rm -rf "$SIDECAR_DIR/vendor/exiftool"
mkdir -p "$SIDECAR_DIR/vendor/exiftool"
# Copy the binary + the libexec tree exiftool needs.
cp "$EXIFTOOL_REAL" "$SIDECAR_DIR/vendor/exiftool/exiftool"
if [[ -d "$EXIFTOOL_PREFIX/libexec/lib" ]]; then
  cp -R "$EXIFTOOL_PREFIX/libexec/lib" "$SIDECAR_DIR/vendor/exiftool/lib"
fi
if [[ -d "$EXIFTOOL_PREFIX/libexec/exiftool" ]]; then
  cp -R "$EXIFTOOL_PREFIX/libexec/exiftool" "$SIDECAR_DIR/vendor/exiftool/exiftool_perl"
fi

# 2b) Stage static ffmpeg + ffprobe (evermeet.cx universal builds).
# These are required by facetag/extract.py — without them, Ellie's
# non-developer Mac can't probe or decode video frames.
FFMPEG_DIR="$SIDECAR_DIR/vendor/ffmpeg"
if [[ ! -x "$FFMPEG_DIR/ffmpeg" || ! -x "$FFMPEG_DIR/ffprobe" ]]; then
  echo "Downloading static ffmpeg + ffprobe from evermeet.cx…"
  rm -rf "$FFMPEG_DIR"
  mkdir -p "$FFMPEG_DIR"
  curl -fsSL "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"  -o "$FFMPEG_DIR/ffmpeg.zip"
  curl -fsSL "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" -o "$FFMPEG_DIR/ffprobe.zip"
  (cd "$FFMPEG_DIR" && unzip -oq ffmpeg.zip && unzip -oq ffprobe.zip)
  rm -f "$FFMPEG_DIR"/*.zip
  if [[ ! -x "$FFMPEG_DIR/ffmpeg" || ! -x "$FFMPEG_DIR/ffprobe" ]]; then
    echo "ERROR: ffmpeg or ffprobe missing after unzip from evermeet.cx" >&2
    exit 1
  fi
  chmod +x "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe"
  # Strip evermeet's developer signature and ad-hoc re-sign so all
  # binaries in the .app have consistent signing identity. Without this
  # macOS might reject the binary even with our library-validation
  # entitlements.
  codesign --force --sign - "$FFMPEG_DIR/ffmpeg"
  codesign --force --sign - "$FFMPEG_DIR/ffprobe"
fi

# 3) Run PyInstaller.
cd "$SIDECAR_DIR"
rm -rf build dist
"$PYINSTALLER" --noconfirm --clean spotted_sidecar.spec

# 4) Place the binary where Tauri expects it. Mac arch → Rust target triple.
ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  TARGET="aarch64-apple-darwin" ;;
  x86_64) TARGET="x86_64-apple-darwin" ;;
  *)      echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

DEST="$REPO/app/src-tauri/binaries"
mkdir -p "$DEST"
cp "dist/spotted-sidecar" "$DEST/spotted-sidecar-$TARGET"
chmod +x "$DEST/spotted-sidecar-$TARGET"

echo
echo "Built: $DEST/spotted-sidecar-$TARGET"
echo "Size:  $(du -sh "$DEST/spotted-sidecar-$TARGET" | cut -f1)"
echo
echo "Next: add to tauri.conf.json bundle.externalBin and rebuild the app."
