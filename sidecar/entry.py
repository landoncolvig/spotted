"""Entry point for the PyInstaller-bundled `spotted-sidecar` binary.

The Tauri app spawns this binary with the same args the facetag CLI takes:

    spotted-sidecar scan /path/to/folder
    spotted-sidecar cluster
    spotted-sidecar label-web --port 8765
    spotted-sidecar tag-write

When frozen by PyInstaller, model files and exiftool live alongside the
binary under sys._MEIPASS. We point InsightFace at that path so the
bundled .app doesn't need to download anything on first launch.
"""
from __future__ import annotations

import os
import sys


def _wire_frozen_paths() -> None:
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    # InsightFace looks for models under ${root}/models/${name}/
    insightface_root = os.path.join(base, "insightface_root")
    if os.path.isdir(insightface_root):
        os.environ["INSIGHTFACE_HOME"] = insightface_root
    # Bundled exiftool
    bundled_exiftool = os.path.join(base, "exiftool")
    if os.path.isfile(bundled_exiftool):
        os.environ["PATH"] = bundled_exiftool + os.pathsep + os.path.dirname(bundled_exiftool) + os.pathsep + os.environ.get("PATH", "")


def main() -> None:
    _wire_frozen_paths()
    # Defer import so env vars are set first.
    from facetag.cli import app
    app()


if __name__ == "__main__":
    main()
