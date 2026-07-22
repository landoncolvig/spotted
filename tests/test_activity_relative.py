"""Relative tag selection: the fix for 'every tag on every clip' over-tagging.

relative_keep must (a) suppress a tag that scores high on a clip only because
that tag scores high everywhere, (b) suppress every tag on a clip that scores
medium on everything, and (c) still keep a genuine per-clip standout — while
keeping far fewer pairs than a flat absolute threshold.
"""
from __future__ import annotations

import numpy as np

from facetag.activity import relative_keep

# tags:   restaurant  casino  graduation
M = np.array([
    [0.25, 0.13, 0.10],   # clip0 restaurant — strong match on restaurant
    [0.10, 0.14, 0.19],   # clip1 graduation — graduation stands out
    [0.16, 0.15, 0.16],   # clip2 busy clip, medium on everything — should get nothing
    [0.11, 0.16, 0.12],   # clip3 casino scores highest here
], dtype=np.float32)


def test_no_over_tagging_vs_flat_threshold():
    keep = relative_keep(M)
    flat = M >= 0.10
    assert keep.sum() < flat.sum()          # far fewer than the flat-0.10 baseline
    assert keep.sum() == 3                   # one tag each on clip0/1/3, none on clip2


def test_strong_match_kept():
    keep = relative_keep(M)
    assert keep[0, 0]                        # restaurant on the restaurant clip (0.25 >= strong)


def test_standout_kept_on_its_clip_only():
    keep = relative_keep(M)
    assert keep[1, 2]                        # graduation on clip1
    assert not keep[:, 2].sum() > 1          # graduation nowhere else


def test_busy_clip_gets_nothing():
    keep = relative_keep(M)
    assert keep[2].sum() == 0                # a clip medium-on-everything collects no tags


def test_tag_high_everywhere_is_not_stamped_everywhere():
    # casino is above floor on several clips but only lands where it stands out,
    # never on all of them — the exact over-tagging Ellie reported.
    keep = relative_keep(M)
    assert keep[:, 1].sum() < M.shape[0]


def test_floor_blocks_pure_noise():
    low = np.full((5, 3), 0.08, dtype=np.float32)   # everything below floor
    assert relative_keep(low).sum() == 0


def test_few_clips_fall_back_to_floor():
    two = np.array([[0.25, 0.05], [0.10, 0.20]], dtype=np.float32)
    keep = relative_keep(two, floor=0.15)
    assert keep.tolist() == [[True, False], [False, True]]
