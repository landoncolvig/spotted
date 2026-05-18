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

a = Analysis(
    ["entry.py"],
    pathex=[os.path.abspath("..")],  # so `import facetag` resolves to the sibling package
    binaries=[
        *onnxruntime_binaries,
        *insightface_binaries,
        *cv2_binaries,
        *sklearn_binaries,
        *hdbscan_binaries,
        *exiftool_binaries,
    ],
    datas=[
        *insightface_datas,
        *onnxruntime_datas,
        *sklearn_datas,
        *hdbscan_datas,
        *cv2_datas,
        *model_datas,
        *copy_metadata("facetag"),
        *copy_metadata("typer"),
        *copy_metadata("rich"),
    ],
    hiddenimports=[
        *insightface_hidden,
        *onnxruntime_hidden,
        *sklearn_hidden,
        *hdbscan_hidden,
        *cv2_hidden,
        *collect_submodules("facetag"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim the bundle — these are huge and we don't need them.
        "matplotlib",
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
    [],
    exclude_binaries=True,
    name="spotted-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # CLI tool; Tauri captures stdout/stderr
    disable_windowed_traceback=False,
    target_arch=None,  # Use host arch; build.sh handles cross-arch separately
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="spotted-sidecar",
)
