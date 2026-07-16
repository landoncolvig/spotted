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


# Each entry: (descriptive subject, output tag).
# The subject is a noun phrase ("kids playing", "a baby") that reads
# naturally when substituted into TEMPLATES below — we don't write full
# captions any more because ensembling them across templates is the
# standard CLIP zero-shot trick that significantly improves
# discrimination vs single-prompt encoding.
CURATED_PROMPTS: list[tuple[str, str]] = [
    # People / settings
    ("kids playing", "kids"),
    ("a baby", "baby"),
    ("a group of adults", "adults"),
    ("a couple together", "couple"),
    ("a large gathering of people", "group"),
    # Indoors / outdoors
    ("an indoor scene at home", "indoors"),
    ("an outdoor scene", "outdoors"),
    ("a nighttime scene", "night"),
    ("a sunset", "sunset"),
    # Places
    ("a beach", "beach"),
    ("a swimming pool", "pool"),
    ("mountains", "mountains"),
    ("a city downtown", "city"),
    ("a backyard", "backyard"),
    ("a restaurant interior", "restaurant"),
    # Activities
    ("people eating a meal", "eating"),
    ("people dancing", "dancing"),
    ("people playing music", "music"),
    ("someone playing a sport", "sports"),
    ("someone swimming", "swimming"),
    ("someone hiking", "hiking"),
    ("someone cooking food", "cooking"),
    ("someone driving a car", "driving"),
    # Occasions
    ("a wedding ceremony", "wedding"),
    ("a birthday party with cake", "birthday"),
    ("a holiday celebration", "celebration"),
    ("christmas decorations", "christmas"),
    ("a graduation ceremony", "graduation"),
    # Faith / church + conference footage — this is the primary library
    # Spotted was built for (worship services, baptisms, conference talks),
    # so the curated set carries scenes a generic CLIP prompt list misses.
    ("a church worship service", "worship"),
    ("a baptism", "baptism"),
    ("a pastor preaching at a pulpit", "preaching"),
    ("a choir singing", "choir"),
    ("a large church congregation", "congregation"),
    ("people praying with bowed heads", "prayer"),
    ("a speaker presenting on a stage at a conference", "conference"),
    ("a person speaking into a microphone to an audience", "speaker"),
    # Objects / vibes
    ("a pet dog", "dog"),
    ("a pet cat", "cat"),
    ("food on a plate", "food"),
    ("a birthday cake with candles", "cake"),
    ("aerial drone footage from above", "drone"),
    ("a close-up portrait of a person's face", "portrait"),
    ("a scenic landscape", "landscape"),
    ("a car or vehicle", "car"),
]

# Prompt-ensemble templates. We encode each subject in all of these and
# average the L2-normalized embeddings (then re-normalize). This is the
# trick that pushed OpenAI CLIP's zero-shot ImageNet accuracy ~4 points
# without changing the model — averaging neutralizes per-template bias
# and concentrates the signal that's actually about the subject.
PROMPT_TEMPLATES: list[str] = [
    "a photo of {}",
    "a video of {}",
    "a frame from a video showing {}",
    "footage of {}",
    "a home video of {}",
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


def encode_tag_embeddings(
    encoder: _clip.ClipEncoder,
    prompts: Iterable[tuple[str, str]] = CURATED_PROMPTS,
    templates: Iterable[str] = PROMPT_TEMPLATES,
) -> np.ndarray:
    """Encode each tag's subject across all templates and average.

    For N tags × T templates, encode N*T strings in a single text batch,
    L2-normalize each, reshape to (N, T, D), mean across templates, then
    L2-normalize the per-tag mean. Returns (N, D) ready to dot against
    frame embeddings.
    """
    prompts_list = list(prompts)
    templates_list = list(templates)
    subjects = [p for p, _ in prompts_list]

    captions: list[str] = []
    for subject in subjects:
        for tmpl in templates_list:
            captions.append(tmpl.format(subject))
    mat = encoder.encode_texts(captions)  # (N*T, D), already L2-normalized

    D = mat.shape[1]
    grouped = mat.reshape(len(subjects), len(templates_list), D)
    averaged = grouped.mean(axis=1)
    return _normalize_rows(averaged)


def apply_auto_tags(
    conn: sqlite3.Connection,
    encoder: _clip.ClipEncoder,
    *,
    threshold: float = 0.13,
    max_tags_per_video: int = 3,
    margin: float = 0.01,
    use_median_margin: bool = True,
    prompts: Iterable[tuple[str, str]] = CURATED_PROMPTS,
) -> dict[str, list[tuple[str, float]]]:
    """Score every video with frame embeddings against `prompts`, write the
    auto_tags table, return what landed for the caller to display.

    Filters that protect against false positives:

    - `threshold`: absolute minimum cosine. Below this, the tag isn't applied
      even if it's a video's top score. This is the primary precision knob.
    - `margin` (only when `use_median_margin`): a tag must beat the per-video
      median score by at least this amount. Kills the "everything scores ~0.10,
      take top-K anyway" pattern that shows up when scoring a large fixed prompt
      list where a clip matches one prompt strongly and dozens weakly.

    `use_median_margin=False` is the mode for a small user-supplied vocabulary
    (the tags the person actually typed). There the median trick backfires:
    with 3 tags, "beat the median" cuts the bottom tag even when it's genuinely
    present, and lets a top tag through on a clip that has none of them. For a
    user's own tags we want a pure per-tag absolute-threshold decision — does
    THIS tag appear in THIS clip, independent of the others.

    Returns {video_path: [(tag, score), ...]} sorted highest score first.
    """
    prompts_list = list(prompts)
    if not prompts_list:
        return {}
    tag_names = [t for _, t in prompts_list]
    prompt_mat = encode_tag_embeddings(encoder, prompts_list)

    out: dict[str, list[tuple[str, float]]] = {}
    for vid, path in _db.videos_with_embeddings(conn):
        rows = _db.load_frame_embeddings(conn, vid)
        if not rows:
            continue
        frame_mat = np.stack([_clip.embedding_from_bytes(b) for _, b in rows])
        frame_mat = _normalize_rows(frame_mat)
        scores = score_video(frame_mat, prompt_mat)
        median = float(np.median(scores))

        picks = sorted(
            (
                (tag_names[i], float(scores[i]))
                for i in range(len(scores))
                if scores[i] >= threshold
                and (not use_median_margin or scores[i] >= median + margin)
            ),
            key=lambda x: -x[1],
        )[:max_tags_per_video]

        _db.replace_auto_tags(conn, vid, picks)
        if picks:
            out[path] = picks
    return out
