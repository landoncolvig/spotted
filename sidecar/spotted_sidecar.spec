# PyInstaller spec for the Spotted sidecar binary.
#
# Usage (from repo root):
#   cd sidecar
#   ./build.sh
#
# Output:
#   sidecar/dist/spotted-sidecar             (raw binary)
#   app/src-tauri/binaries/spotted-sidecar-aarch64-apple-darwin   (copied for Tauri)
#
# Tauri expects sidecar binaries to be suffixed with the Rust target triple.

# ruff: noqa
# type: ignore
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# InsightFace + ONNX runtime: aggressively collect everything to avoid
# missing-import errors at runtime.
insightface_datas, insightface_binaries, insightface_hidden = collect_all("insightface")
onnxruntime_datas, onnxruntime_binaries, onnxruntime_hidden = collect_all("onnxruntime")
sklearn_datas, sklearn_binaries, sklearn_hidden = collect_all("sklearn")
hdbscan_datas, hdbscan_binaries, hdbscan_hidden = collect_all("hdbscan")
cv2_datas, cv2_binaries, cv2_hidden = collect_all("cv2")
# MobileCLIP via Core ML for zero-shot activity tagging.
coremltools_datas, coremltools_binaries, coremltools_hidden = collect_all("coremltools")
# ftfy + regex back the slim CLIP BPE tokenizer in facetag/clip_tokenizer.py
# (replaces a ~50MB transformers dependency we used to pull just for one
# CLIPTokenizer.from_pretrained call).
ftfy_datas, ftfy_binaries, ftfy_hidden = collect_all("ftfy")
regex_datas, regex_binaries, regex_hidden = collect_all("regex")

# Bundle the pre-downloaded buffalo_l model from the user's ~/.insightface.
# build.sh ensures this exists before pyinstaller runs.
insightface_home = os.path.expanduser("~/.insightface")
model_datas = [
    (os.path.join(insightface_home, "models", "buffalo_l"), "insightface_root/models/buffalo_l"),
]

# Bundle exiftool (Perl + scripts). build.sh stages a copy under sidecar/vendor/exiftool.
exiftool_dir = os.path.abspath("vendor/exiftool")
exiftool_binaries = []
if os.path.isdir(exiftool_dir):
    for root, _dirs, files in os.walk(exiftool_dir):
        rel = os.path.relpath(root, exiftool_dir)
        for fn in files:
            src = os.path.join(root, fn)
            dest_dir = "exiftool" if rel == "." else os.path.join("exiftool", rel)
            exiftool_binaries.append((src, dest_dir))

# Bundle ffmpeg + ffprobe. build.sh downloads static builds from evermeet.cx.
ffmpeg_dir = os.path.abspath("vendor/ffmpeg")
ffmpeg_binaries = []
if os.path.isdir(ffmpeg_dir):
    for fn in ("ffmpeg", "ffprobe"):
        src = os.path.join(ffmpeg_dir, fn)
        if os.path.isfile(src):
            ffmpeg_binaries.append((src, "ffmpeg"))

# Bundle MobileCLIP-S2 Core ML packages. build.sh downloads these from
# Hugging Face (apple/coreml-mobileclip) into vendor/mobileclip/ if missing.
# The .mlpackages are directories on disk; we have to walk them so each
# file lands at the expected relative path under mobileclip/ at runtime.
mobileclip_dir = os.path.abspath("vendor/mobileclip")
mobileclip_datas = []
if os.path.isdir(mobileclip_dir):
    for root, _dirs, files in os.walk(mobileclip_dir):
        rel = os.path.relpath(root, mobileclip_dir)
        for fn in files:
            src = os.path.join(root, fn)
            dest_dir = "mobileclip" if rel == "." else os.path.join("mobileclip", rel)
            mobileclip_datas.append((src, dest_dir))

# Bundle the CLIP BPE tokenizer files so clip.py can load them via
# CLIPTokenizer.from_pretrained(<bundled path>) without ever hitting the
# Hugging Face hub at runtime.
clip_tok_dir = os.path.abspath("vendor/clip_tokenizer")
clip_tok_datas = []
if os.path.isdir(clip_tok_dir):
    for fn in os.listdir(clip_tok_dir):
        src = os.path.join(clip_tok_dir, fn)
        if os.path.isfile(src):
            clip_tok_datas.append((src, "clip_tokenizer"))

a = Analysis(
    ["entry.py"],
    pathex=[os.path.abspath("..")],  # so `import facetag` resolves to the sibling package
    binaries=[
        *onnxruntime_binaries,
        *insightface_binaries,
        *cv2_binaries,
        *sklearn_binaries,
        *hdbscan_binaries,
        *coremltools_binaries,
        *ftfy_binaries,
        *regex_binaries,
        *exiftool_binaries,
        *ffmpeg_binaries,
    ],
    datas=[
        *insightface_datas,
        *onnxruntime_datas,
        *sklearn_datas,
        *hdbscan_datas,
        *cv2_datas,
        *coremltools_datas,
        *ftfy_datas,
        *regex_datas,
        *model_datas,
        *mobileclip_datas,
        *clip_tok_datas,
        *copy_metadata("facetag"),
        *copy_metadata("typer"),
        *copy_metadata("rich"),
        *copy_metadata("coremltools"),
    ],
    hiddenimports=[
        *insightface_hidden,
        *onnxruntime_hidden,
        *sklearn_hidden,
        *hdbscan_hidden,
        *cv2_hidden,
        *coremltools_hidden,
        *ftfy_hidden,
        *regex_hidden,
        *collect_submodules("facetag"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim the bundle — these are huge and we don't need them.
        # NOTE: matplotlib stays in. Something in the insightface/sklearn
        # transitive graph imports it at runtime.
        "PySide6",
        "PyQt6",
        "PyQt5",
        "tkinter",
        "IPython",
        "jupyter",
        "notebook",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="spotted-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,  # CLI tool; Tauri captures stdout/stderr
    disable_windowed_traceback=False,
    target_arch=None,  # Host arch; CI builds universal by stitching arch binaries
    codesign_identity=None,
    entitlements_file=None,
)
