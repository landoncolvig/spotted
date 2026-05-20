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

# Homebrew's exiftool patches the Perl script with absolute @INC paths into
# its own Cellar (e.g. /opt/homebrew/Cellar/exiftool/13.55/libexec/lib/perl5).
# On any Mac without Homebrew (i.e. our testers), those paths don't exist —
# exiftool runs but immediately dies with "Can't locate Image/ExifTool.pm in
# @INC". The cli.py orchestrator currently swallows that error per-video, so
# the app shows a successful "Done" while NOTHING gets written. Rewrite the
# Cellar paths to use the $exeDir variable the script already computes in
# its BEGIN block, so @INC resolves relative to wherever the binary lives.
# `cp` preserves Homebrew's read-only mode (0444); make it writable for the
# in-place rewrite, then put it back to executable.
chmod u+rw "$SIDECAR_DIR/vendor/exiftool/exiftool"
python3 - "$SIDECAR_DIR/vendor/exiftool/exiftool" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path).read()
patched = re.sub(
    r'unshift @INC, "/opt/homebrew/Cellar/exiftool/[^"]*/libexec/lib/perl5([^"]*)"',
    r'unshift @INC, "$exeDir/lib/perl5\1"',
    src,
)
if patched == src:
    print("WARN: exiftool @INC patch matched nothing — Homebrew layout may have changed", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(patched)
print("Patched exiftool @INC paths to be Homebrew-independent.")
PY
chmod +x "$SIDECAR_DIR/vendor/exiftool/exiftool"

# 2a) Stage Apple's MobileCLIP-S2 Core ML packages for activity tagging.
# Downloaded once from Hugging Face into sidecar/vendor/mobileclip/. The
# .mlpackages are ~190MB total; we only re-fetch if missing so CI cache
# hits make subsequent builds fast.
MOBILECLIP_DIR="$SIDECAR_DIR/vendor/mobileclip"
if [[ ! -d "$MOBILECLIP_DIR/mobileclip_s2_image.mlpackage" || ! -d "$MOBILECLIP_DIR/mobileclip_s2_text.mlpackage" ]]; then
  echo "Downloading Apple's MobileCLIP-S2 Core ML packages…"
  mkdir -p "$MOBILECLIP_DIR"
  "$PYTHON" - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="apple/coreml-mobileclip",
    allow_patterns=["mobileclip_s2*"],
    local_dir="$MOBILECLIP_DIR",
)
PY
fi

# 2a-bis) Stage the CLIP BPE tokenizer files so the bundled sidecar
# doesn't try to hit the Hugging Face hub at runtime. clip.py loads
# them from this local directory via CLIPTokenizer.from_pretrained(path).
CLIP_TOK_DIR="$SIDECAR_DIR/vendor/clip_tokenizer"
if [[ ! -f "$CLIP_TOK_DIR/tokenizer.json" && ! -f "$CLIP_TOK_DIR/vocab.json" ]]; then
  echo "Downloading CLIP tokenizer files…"
  mkdir -p "$CLIP_TOK_DIR"
  "$PYTHON" - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="openai/clip-vit-base-patch32",
    allow_patterns=["tokenizer*", "vocab*", "merges*", "special_tokens_map*"],
    local_dir="$CLIP_TOK_DIR",
)
PY
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

# 5) Ad-hoc sign the sidecar WITH entitlements before Tauri picks it up.
# PyInstaller embeds Python.framework, which carries python.org's Team ID.
# Without disable-library-validation, the ad-hoc-signed sidecar can't dlopen
# Python at runtime ("(non-platform) have different Team IDs"). Tauri's
# bundler signs the parent .app with entitlements but does NOT propagate
# them to sidecar binaries, so we do it here.
ENTITLEMENTS="$REPO/app/src-tauri/entitlements.plist"
if [[ -f "$ENTITLEMENTS" ]]; then
  codesign --force --sign - --options runtime \
    --entitlements "$ENTITLEMENTS" \
    "$DEST/spotted-sidecar-$TARGET"
else
  echo "WARN: $ENTITLEMENTS not found; sidecar will fail to load Python at runtime." >&2
fi

echo
echo "Built: $DEST/spotted-sidecar-$TARGET"
echo "Size:  $(du -sh "$DEST/spotted-sidecar-$TARGET" | cut -f1)"
echo
echo "Next: add to tauri.conf.json bundle.externalBin and rebuild the app."
