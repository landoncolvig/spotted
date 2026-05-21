"""MobileCLIP image + text encoder wrapper.

Loads Apple's MobileCLIP-S2 Core ML models for zero-shot activity detection:
the image encoder embeds sampled frames; the text encoder embeds curated
prompts ("kids playing outside", "wedding celebration", etc.). Cosine
similarity between the two ranks how strongly each prompt matches a frame
or a whole video.

Why MobileCLIP + Core ML:
- Open weights (Apache 2.0) so we satisfy the no-API constraint.
- Routes to the Apple Neural Engine via Core ML on Apple Silicon, so
  inference is ~3ms per frame with negligible CPU contention against
  the existing InsightFace pipeline.
- Total model footprint ~190MB (image + text).

This module degrades gracefully: if the model files aren't present, the
encoder raises ClipUnavailable and the scan loop logs+skips embeddings
without breaking face detection.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np


class ClipUnavailable(RuntimeError):
    """Raised when the MobileCLIP model isn't bundled or coremltools can't load it."""


def _model_root() -> Optional[Path]:
    """Resolve where the MobileCLIP .mlpackages live.

    Three lookup orders, first hit wins:
    1. SPOTTED_MOBILECLIP env var (developer override).
    2. PyInstaller bundle: <_MEIPASS>/mobileclip/.
    3. Source-tree dev location: sidecar/vendor/mobileclip/.
    """
    env = os.environ.get("SPOTTED_MOBILECLIP")
    if env and Path(env).is_dir():
        return Path(env)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / "mobileclip"
        if cand.is_dir():
            return cand
    here = Path(__file__).resolve().parent.parent
    cand = here / "sidecar" / "vendor" / "mobileclip"
    if cand.is_dir():
        return cand
    return None


# _tokenizer_root removed: the slim SimpleTokenizer in clip_tokenizer.py
# resolves its own bpe_simple_vocab_16e6.txt.gz via SPOTTED_CLIP_VOCAB,
# the PyInstaller bundle, or the dev tree.


class ClipEncoder:
    """Lazy wrapper around the two MobileCLIP-S2 Core ML models + tokenizer."""

    def __init__(self) -> None:
        self._img_model = None
        self._txt_model = None
        self._tokenizer = None
        self._root = _model_root()

    @property
    def available(self) -> bool:
        return self._root is not None

    def _load(self) -> None:
        if self._img_model is not None:
            return
        if self._root is None:
            raise ClipUnavailable(
                "MobileCLIP model directory not found. Set SPOTTED_MOBILECLIP or "
                "place the .mlpackages under sidecar/vendor/mobileclip/."
            )
        try:
            import coremltools as ct
        except Exception as e:
            raise ClipUnavailable(f"coremltools import failed: {e}") from e

        img_path = self._root / "mobileclip_s2_image.mlpackage"
        txt_path = self._root / "mobileclip_s2_text.mlpackage"
        if not img_path.is_dir() or not txt_path.is_dir():
            raise ClipUnavailable(
                f"Missing MobileCLIP packages under {self._root}: "
                f"need mobileclip_s2_image.mlpackage and mobileclip_s2_text.mlpackage."
            )
        # CPU_AND_NE lets Core ML pick the Neural Engine when available
        # and fall back to CPU on Intel Macs. ALL would include the GPU,
        # but ANE is faster + cooler for this workload.
        units = ct.ComputeUnit.CPU_AND_NE
        self._img_model = ct.models.MLModel(str(img_path), compute_units=units)
        self._txt_model = ct.models.MLModel(str(txt_path), compute_units=units)

        try:
            from .clip_tokenizer import SimpleTokenizer
        except Exception as e:
            raise ClipUnavailable(
                f"CLIP tokenizer unavailable: {e}"
            ) from e
        # Pure-Python BPE tokenizer ported from OpenAI's CLIP repo.
        # Replaces a ~50MB transformers dependency we previously pulled
        # just to call CLIPTokenizer.from_pretrained.
        self._tokenizer = SimpleTokenizer()

    def encode_image(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Encode a single BGR frame ndarray. Returns L2-normalized 512-dim float32."""
        self._load()
        from PIL import Image

        # BGR → RGB → 256x256 PIL.Image is what the .mlpackage expects.
        rgb = frame_bgr[..., ::-1]
        img = Image.fromarray(rgb).resize((256, 256), Image.BICUBIC)
        out = self._img_model.predict({"image": img})
        emb = out["final_emb_1"][0].astype(np.float32)
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb

    def encode_texts(self, prompts: list[str]) -> np.ndarray:
        """Encode a list of text prompts. Returns L2-normalized (N, 512) float32."""
        self._load()
        # SimpleTokenizer.tokenize returns (N, 77) int32; the Core ML model
        # expects one row at a time so we slice per-prompt.
        all_ids = self._tokenizer.tokenize(prompts)  # (N, 77) int32
        embs: list[np.ndarray] = []
        for row in range(all_ids.shape[0]):
            ids = all_ids[row : row + 1]  # (1, 77) — model's expected shape
            out = self._txt_model.predict({"text": ids})
            emb = out["final_emb_1"][0].astype(np.float32)
            norm = np.linalg.norm(emb)
            embs.append(emb / norm if norm > 0 else emb)
        return np.stack(embs)


def embedding_to_bytes(emb: np.ndarray) -> bytes:
    """Pack a (512,) float32 embedding into float16 bytes for compact storage."""
    return emb.astype(np.float16).tobytes()


def embedding_from_bytes(blob: bytes) -> np.ndarray:
    """Unpack float16 BLOB back to (512,) float32."""
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)
