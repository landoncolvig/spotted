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

# Map a bare user tag to the curated, more-specific subject phrase when we have
# one — "pool" -> "a swimming pool", "wedding" -> "a wedding ceremony", "baby"
# -> "a baby". CLIP is far more accurate on a concrete noun phrase than a lone
# ambiguous word (a bare "pool" also matches pool tables and puddles), which is
# why the curated list uses phrases. User-typed tags get the same benefit.
CURATED_BY_TAG: dict[str, str] = {tag: subject for subject, tag in CURATED_PROMPTS}


def enrich_tag(tag: str) -> str:
    """The best CLIP subject phrase for a user tag: the curated description when
    we have one, else the tag exactly as typed."""
    return CURATED_BY_TAG.get(tag.strip().lower(), tag)


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


# Relative-selection defaults, calibrated on real footage with MobileCLIP-S2.
# CLIP cosine scores here are low and compressed (~0.06–0.26) and, worse,
# uncalibrated across tags: "casino" averages ~0.135 on footage with no casino,
# as high as "restaurant" on footage that IS a restaurant. So a flat absolute
# threshold either tags everything (0.10 → ~5.6 clips/tag — the over-tagging
# users hit) or nothing. Two independent biases cause it: some TAGS score high
# everywhere, and some CLIPS score high on everything. We neutralize both by
# requiring a (clip, tag) match to stand out on BOTH axes.
REL_FLOOR = 0.15       # absolute minimum cosine; kills pure noise
REL_STRONG = 0.24      # unambiguous match — keep regardless of the relative tests
REL_K_TAG = 1.0        # per-tag: score must beat that tag's mean by k standard devs
REL_CLIP_MARGIN = 0.03  # per-clip: score must beat this clip's median tag-score by this


def relative_keep(
    M: np.ndarray,
    *,
    floor: float = REL_FLOOR,
    strong: float = REL_STRONG,
    k_tag: float = REL_K_TAG,
    clip_margin: float = REL_CLIP_MARGIN,
) -> np.ndarray:
    """Boolean (video × tag) keep-mask for relative tag selection.

    Keep (v, t) iff score >= floor AND it stands out for tag t (above t's
    mean + k_tag·std across clips) AND EITHER it also stands out on clip v
    (above v's median tag-score + clip_margin) OR it's a strong absolute match.

    The per-tag stand-out is required even for a strong match: otherwise a tag
    CLIP loves — "baby" on family footage, say, scoring high almost everywhere —
    clears the strong bar on every clip and gets stamped on all of them. Strong
    only lets a distinctive tag skip the per-CLIP test (a busy clip that scores
    high on several tags), not the per-tag one. With fewer than 3 clips the
    per-tag distribution is unreliable, so fall back to the floor alone.
    """
    M = np.asarray(M, dtype=np.float32)
    keep = M >= floor
    if M.shape[0] >= 3:
        tag_cut = M.mean(axis=0) + k_tag * M.std(axis=0)          # (T,) per-tag
        clip_cut = (np.median(M, axis=1) + clip_margin)[:, None]  # (V, 1) per-clip
        per_tag = M >= tag_cut
        per_clip = M >= clip_cut
        keep &= per_tag & (per_clip | (M >= strong))
    return keep


def apply_auto_tags(
    conn: sqlite3.Connection,
    encoder: _clip.ClipEncoder,
    *,
    threshold: float = 0.13,
    max_tags_per_video: int = 3,
    margin: float = 0.01,
    use_median_margin: bool = True,
    prompts: Iterable[tuple[str, str]] = CURATED_PROMPTS,
    relative: bool = False,
    floor: float = REL_FLOOR,
    strong: float = REL_STRONG,
    k_tag: float = REL_K_TAG,
    clip_margin: float = REL_CLIP_MARGIN,
) -> dict[str, list[tuple[str, float]]]:
    """Score every video with frame embeddings against `prompts`, write the
    auto_tags table, return what landed for the caller to display.

    Two selection modes:

    - Legacy absolute (`relative=False`): a tag lands on a clip if its cosine
      clears `threshold` (and, when `use_median_margin`, beats that clip's
      median by `margin`). Kept for the fixed curated prompt list.

    - Relative (`relative=True`, used for the user's own typed tags): a tag
      lands on a clip only if the score clears `floor` AND is either a `strong`
      absolute match OR stands out on BOTH axes — above that tag's own
      mean+`k_tag`·std across the library (so a tag that scores high everywhere,
      like "casino", doesn't get applied everywhere) and above that clip's
      median tag-score by `clip_margin` (so a clip that scores high on
      everything doesn't collect every tag). This is what stops the
      every-tag-on-every-clip over-tagging while still putting each tag on the
      clips it actually belongs to.

    Returns {video_path: [(tag, score), ...]} sorted highest score first.
    """
    prompts_list = list(prompts)
    if not prompts_list:
        return {}
    tag_names = [t for _, t in prompts_list]
    prompt_mat = encode_tag_embeddings(encoder, prompts_list)

    # Pass 1: score every video, holding the full (video × tag) matrix so the
    # relative mode can compute per-tag statistics across the library.
    entries: list[tuple[int, str, np.ndarray]] = []
    for vid, path in _db.videos_with_embeddings(conn):
        rows = _db.load_frame_embeddings(conn, vid)
        if not rows:
            continue
        frame_mat = _normalize_rows(np.stack([_clip.embedding_from_bytes(b) for _, b in rows]))
        entries.append((vid, path, score_video(frame_mat, prompt_mat)))
    if not entries:
        return {}

    out: dict[str, list[tuple[str, float]]] = {}

    if relative:
        M = np.array([s for _, _, s in entries], dtype=np.float32)   # (n_vid, n_tags)
        keep = relative_keep(M, floor=floor, strong=strong, k_tag=k_tag, clip_margin=clip_margin)
        for i, (vid, path, scores) in enumerate(entries):
            picks = sorted(
                ((tag_names[j], float(scores[j])) for j in range(len(tag_names)) if keep[i, j]),
                key=lambda x: -x[1],
            )[:max_tags_per_video]
            _db.replace_auto_tags(conn, vid, picks)
            if picks:
                out[path] = picks
        return out

    for vid, path, scores in entries:
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
