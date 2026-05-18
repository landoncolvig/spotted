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
        import onnxruntime as ort
        from insightface.app import FaceAnalysis

        providers = ort.get_available_providers()
        # CoreML first on Apple Silicon, otherwise CPU.
        ordered = []
        if "CoreMLExecutionProvider" in providers:
            ordered.append("CoreMLExecutionProvider")
        ordered.append("CPUExecutionProvider")

        app = FaceAnalysis(name="buffalo_l", providers=ordered)
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
