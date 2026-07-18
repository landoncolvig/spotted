"""InsightFace wrapper. Returns bbox + 512-d normalized embedding per face."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Face:
    bbox: tuple[int, int, int, int]  # x, y, w, h
    embedding: np.ndarray  # (512,) float32, L2-normalized
    det_score: float


class Detector:
    """Lazy-loaded InsightFace buffalo_l (RetinaFace + ArcFace).

    Uses CoreMLExecutionProvider on Apple Silicon if available, falls back to CPU.
    """

    def __init__(self, det_size: int = 640, min_score: float = 0.5):
        self.det_size = det_size
        self.min_score = min_score
        self._app = None

    def _load(self):
        if self._app is not None:
            return
        import os
        import onnxruntime as ort
        from insightface.app import FaceAnalysis

        providers = ort.get_available_providers()
        # CoreML first on Apple Silicon, otherwise CPU.
        ordered = []
        if "CoreMLExecutionProvider" in providers:
            ordered.append("CoreMLExecutionProvider")
        ordered.append("CPUExecutionProvider")

        # When frozen by PyInstaller (the Spotted .app sidecar), entry.py
        # sets INSIGHTFACE_HOME to the bundled model location. Pass it
        # through so models load offline.
        # facetag only uses detection (bbox + 5-pt kps for ArcFace alignment)
        # and recognition (the 512-d embedding). Restrict the pack to those
        # two so the unused landmark_2d_106 / landmark_3d_68 / genderage models
        # never load. The 3D-landmark model (1k3d68) in particular crashes the
        # frozen sidecar: its meanshape_68.pkl isn't in the bundle, so
        # self.mean_lmk is None and pose estimation raises
        # "AttributeError: 'NoneType' object has no attribute 'shape'" the first
        # time a face is detected. Skipping it also drops ~143MB of model load
        # and speeds up every frame.
        kwargs = {
            "name": "buffalo_l",
            "providers": ordered,
            "allowed_modules": ["detection", "recognition"],
        }
        bundled_root = os.environ.get("INSIGHTFACE_HOME")
        if bundled_root:
            kwargs["root"] = bundled_root

        app = FaceAnalysis(**kwargs)
        app.prepare(ctx_id=0, det_size=(self.det_size, self.det_size))
        self._app = app

    def detect(self, frame_bgr: np.ndarray) -> list[Face]:
        self._load()
        raw = self._app.get(frame_bgr)
        out: list[Face] = []
        for f in raw:
            if float(f.det_score) < self.min_score:
                continue
            x1, y1, x2, y2 = (int(v) for v in f.bbox)
            x, y, w, h = x1, y1, max(1, x2 - x1), max(1, y2 - y1)
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            # Defensive re-normalize.
            n = np.linalg.norm(emb)
            if n > 0:
                emb = emb / n
            out.append(Face(bbox=(x, y, w, h), embedding=emb, det_score=float(f.det_score)))
        return out
