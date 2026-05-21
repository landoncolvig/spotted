"""Integration tests for the activity-detection pipeline.

Only runs when MobileCLIP is available locally (env var, dev tree, or
PyInstaller bundle). Skipped in environments without the .mlpackages
so CI doesn't have to download 200MB of models just to test
auto-tagging behavior.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from facetag import activity as _activity
from facetag import clip as _clip


pytestmark = pytest.mark.skipif(
    not _clip.ClipEncoder().available,
    reason="MobileCLIP model not staged (set SPOTTED_MOBILECLIP or run sidecar/build.sh)",
)


@pytest.fixture(scope="module")
def encoder() -> _clip.ClipEncoder:
    e = _clip.ClipEncoder()
    e._load()
    return e


def test_encode_texts_returns_normalized_512d(encoder: _clip.ClipEncoder) -> None:
    embs = encoder.encode_texts(["a photo of kids", "a wedding ceremony"])
    assert embs.shape == (2, 512)
    assert embs.dtype == np.float32
    # L2-normalized inside the encoder
    norms = np.linalg.norm(embs, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-5)


def test_encode_image_returns_normalized_512d(encoder: _clip.ClipEncoder, tmp_path) -> None:
    """Synthesize a solid-color BGR ndarray (no ffmpeg needed) and run it
    through the image encoder. Output must match shape/dtype/normalization
    contract or the downstream cosine math breaks."""
    frame = np.full((256, 256, 3), 128, dtype=np.uint8)
    emb = encoder.encode_image(frame)
    assert emb.shape == (512,)
    assert emb.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(emb), 1.0, atol=1e-5)


def test_prompt_ensemble_averaging_runs(encoder: _clip.ClipEncoder) -> None:
    """The ensemble path in apply_auto_tags relies on encode_tag_embeddings
    averaging N templates per tag. Smoke-test that path produces the
    expected (n_tags, 512) shape."""
    mat = _activity.encode_tag_embeddings(encoder)
    assert mat.shape == (len(_activity.CURATED_PROMPTS), 512)
    # Each row L2-normalized
    norms = np.linalg.norm(mat, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-5)


def test_score_video_picks_strongest_per_prompt(encoder: _clip.ClipEncoder) -> None:
    """score_video should return the MAX similarity across frames per prompt,
    not the mean or first — otherwise a clip with one strong frame match
    would get filtered out by the average."""
    # Construct two frame embeddings: one neutral, one engineered to lean
    # toward the first prompt's text embedding direction.
    prompts = _activity.encode_tag_embeddings(encoder)  # (N, 512)
    leaning = prompts[0]  # already normalized
    neutral = np.zeros(512, dtype=np.float32)
    neutral[0] = 1.0  # arbitrary unit vector
    frames = np.stack([neutral, leaning])
    scores = _activity.score_video(frames, prompts)
    # The leaning frame matches prompts[0] exactly → max similarity = 1.0
    np.testing.assert_allclose(scores[0], 1.0, atol=1e-4)
    # Other scores should be strictly lower than 1.0
    assert (scores[1:] < 0.99).all()
