"""Relative tag selection: the fix for 'every tag on every clip' over-tagging.

relative_keep must (a) suppress a tag that scores high on a clip only because
that tag scores high everywhere, (b) suppress every tag on a clip that scores
medium on everything, and (c) still keep a genuine per-clip standout — while
keeping far fewer pairs than a flat absolute threshold.
"""
from __future__ import annotations

import numpy as np

from facetag.activity import enrich_tag, relative_keep

# tags:   restaurant  casino  graduation
M = np.array([
    [0.25, 0.13, 0.10],   # clip0 restaurant — strong match on restaurant
    [0.10, 0.14, 0.19],   # clip1 graduation — graduation stands out
    [0.16, 0.15, 0.16],   # clip2 busy clip, medium on everything — should get nothing
    [0.11, 0.16, 0.12],   # clip3 casino scores highest here
], dtype=np.float32)

# These tests exercise the selection LOGIC at the point this matrix was designed
# for; the production default REL_K_TAG is tuned separately (validated on real
# footage), so pin k_tag here rather than couple the tests to that default.
K = 1.0


def test_no_over_tagging_vs_flat_threshold():
    keep = relative_keep(M, k_tag=K)
    flat = M >= 0.10
    assert keep.sum() < flat.sum()          # far fewer than the flat-0.10 baseline
    assert keep.sum() == 3                   # one tag each on clip0/1/3, none on clip2


def test_strong_match_kept():
    keep = relative_keep(M, k_tag=K)
    assert keep[0, 0]                        # restaurant on the restaurant clip (0.25 >= strong)


def test_standout_kept_on_its_clip_only():
    keep = relative_keep(M, k_tag=K)
    assert keep[1, 2]                        # graduation on clip1
    assert not keep[:, 2].sum() > 1          # graduation nowhere else


def test_busy_clip_gets_nothing():
    keep = relative_keep(M, k_tag=K)
    assert keep[2].sum() == 0                # a clip medium-on-everything collects no tags


def test_tag_high_everywhere_is_not_stamped_everywhere():
    # casino is above floor on several clips but only lands where it stands out,
    # never on all of them — the exact over-tagging Ellie reported.
    keep = relative_keep(M, k_tag=K)
    assert keep[:, 1].sum() < M.shape[0]


def test_floor_blocks_pure_noise():
    low = np.full((5, 3), 0.08, dtype=np.float32)   # everything below floor
    assert relative_keep(low).sum() == 0


def test_few_clips_fall_back_to_floor():
    two = np.array([[0.25, 0.05], [0.10, 0.20]], dtype=np.float32)
    keep = relative_keep(two, floor=0.15)
    assert keep.tolist() == [[True, False], [False, True]]


def test_strong_but_not_standout_tag_not_stamped_everywhere():
    # "baby" (col 0) clears the strong bar (>=0.24) on every clip but is only
    # distinctive on clip0. The tightening keeps it where it stands out for the
    # tag, not on all four — the "baby everywhere" complaint.
    M = np.array([
        [0.30, 0.10],
        [0.25, 0.11],
        [0.25, 0.30],
        [0.24, 0.10],
    ], dtype=np.float32)
    keep = relative_keep(M, k_tag=K)
    assert keep[:, 0].sum() < 4     # not on every clip
    assert keep[0, 0]               # kept where it genuinely stands out


def test_coverage_cap_trims_a_blanketing_tag():
    # A tag that would clear the relative bar on most clips (baby/casino/bingo
    # on a Vegas trip) is capped to its top-scoring `max_coverage` fraction.
    # k_tag=0 makes the per-tag test permissive so we isolate the cap.
    M = np.array([[0.10], [0.10], [0.25], [0.26], [0.27], [0.28], [0.29], [0.30]],
                 dtype=np.float32)
    keep = relative_keep(M, floor=0.1, strong=0.99, k_tag=0.0, clip_margin=-1.0, max_coverage=0.5)
    assert keep[:, 0].sum() == 4              # 8 clips × 0.5
    assert keep[7, 0] and keep[6, 0]          # kept the highest-scoring
    assert not keep[2, 0] and not keep[3, 0]  # dropped the weaker ones


def test_enrich_tag_uses_curated_phrase():
    assert enrich_tag("pool") == "a swimming pool"
    assert enrich_tag("wedding") == "a wedding ceremony"
    assert enrich_tag("baby") == "a baby"
    assert enrich_tag("Pool") == "a swimming pool"   # case-insensitive
    assert enrich_tag("kayaking") == "kayaking"       # unknown passes through
