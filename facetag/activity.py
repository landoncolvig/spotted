"""Curated prompts + scoring for zero-shot activity tagging.

For each video that has frame embeddings, we cosine-similarity every curated
prompt against every frame, take the per-video MAX similarity per prompt
(i.e. "did this prompt match the clip at any moment"), and apply any prompt
whose max score crosses a threshold as an auto-tag.

Threshold and prompt list are deliberately conservative — false positives
clutter the editor's keyword column. Easier to start narrow and let users
search free-text for the long tail (planned Phase 2).
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

import numpy as np

from . import clip as _clip
from . import db as _db


# Each entry: (prompt for CLIP, output tag the editor sees).
# Prompts are natural-language because CLIP was trained on captions, not
# single words. Output tags are short so DaVinci's Keywords column stays
# scannable.
CURATED_PROMPTS: list[tuple[str, str]] = [
    # People / settings
    ("a photo of kids playing", "kids"),
    ("a photo of a baby", "baby"),
    ("a group of adults talking", "adults"),
    ("a photo of a couple", "couple"),
    ("a large group of people gathered", "group"),
    # Indoors / outdoors
    ("a photo taken indoors at home", "indoors"),
    ("a photo taken outdoors", "outdoors"),
    ("a photo taken at night", "night"),
    ("a photo of a sunset", "sunset"),
    # Places
    ("a photo at the beach", "beach"),
    ("a photo at a swimming pool", "pool"),
    ("a photo of mountains", "mountains"),
    ("a photo in a city or downtown", "city"),
    ("a photo in a backyard", "backyard"),
    ("a photo at a restaurant", "restaurant"),
    # Activities
    ("a photo of people eating a meal", "eating"),
    ("a photo of people dancing", "dancing"),
    ("a photo of people playing music", "music"),
    ("a photo of someone playing sports", "sports"),
    ("a photo of someone swimming", "swimming"),
    ("a photo of someone hiking", "hiking"),
    ("a photo of someone cooking food", "cooking"),
    ("a photo of someone driving", "driving"),
    # Occasions
    ("a photo of a wedding ceremony", "wedding"),
    ("a photo of a birthday party with cake", "birthday"),
    ("a photo of a holiday celebration", "celebration"),
    ("a photo of christmas decorations", "christmas"),
    ("a photo of a graduation ceremony", "graduation"),
    # Objects / vibes
    ("a photo of a pet dog", "dog"),
    ("a photo of a pet cat", "cat"),
    ("a photo of food on a plate", "food"),
    ("a photo of a birthday cake with candles", "cake"),
    ("drone aerial footage from above", "drone"),
    ("a close-up portrait of a person", "portrait"),
    ("a scenic landscape photo", "landscape"),
    ("a photo of a car or vehicle", "car"),
]


def _normalize_rows(mat: np.ndarray) -> np.ndarray:
    """L2-normalize each row, returning a (N, D) array safe to dot against."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.maximum(norms, 1e-8)


def score_video(
    frame_embs: np.ndarray,
    prompt_embs: np.ndarray,
) -> np.ndarray:
    """For one video's frames, return (n_prompts,) max-over-frames cosine sim.

    Embeddings are L2-normalized so cosine sim is just the dot product.
    """
    if frame_embs.size == 0:
        return np.zeros(prompt_embs.shape[0], dtype=np.float32)
    sims = frame_embs @ prompt_embs.T  # (n_frames, n_prompts)
    return sims.max(axis=0)


def apply_auto_tags(
    conn: sqlite3.Connection,
    encoder: _clip.ClipEncoder,
    *,
    threshold: float = 0.22,
    max_tags_per_video: int = 6,
    prompts: Iterable[tuple[str, str]] = CURATED_PROMPTS,
) -> dict[str, list[tuple[str, float]]]:
    """Score every video with frame embeddings against the curated prompts,
    write the auto_tags table, return what landed for the caller to display.

    Returns {video_path: [(tag, score), ...]} sorted highest score first.
    """
    prompts_list = list(prompts)
    prompt_texts = [p for p, _ in prompts_list]
    tag_names = [t for _, t in prompts_list]
    prompt_mat = encoder.encode_texts(prompt_texts)  # already L2-normalized

    out: dict[str, list[tuple[str, float]]] = {}
    for vid, path in _db.videos_with_embeddings(conn):
        rows = _db.load_frame_embeddings(conn, vid)
        if not rows:
            continue
        frame_mat = np.stack([_clip.embedding_from_bytes(b) for _, b in rows])
        frame_mat = _normalize_rows(frame_mat)
        scores = score_video(frame_mat, prompt_mat)

        # Pick all prompts above threshold, capped at max_tags_per_video so
        # one chatty video doesn't dump 20 tags into its keyword column.
        picks = sorted(
            ((tag_names[i], float(scores[i])) for i in range(len(scores)) if scores[i] >= threshold),
            key=lambda x: -x[1],
        )[:max_tags_per_video]

        _db.replace_auto_tags(conn, vid, picks)
        if picks:
            out[path] = picks
    return out
