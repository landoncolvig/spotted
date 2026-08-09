"""The write phase says where it is, like the scan already did.

The scan has reported "12 of 107 · about 3 min left" for a while. Writing did
not: it showed the current filename and nothing else, over a bar with no
position in it. That phase runs exiftool twice and xattr twice per clip, so on
a few hundred clips it is minutes of movement that could equally be a hang, and
it is the phase where a hang would matter most because it is the one touching
the user's files.

Also covers the skip path. `tag-skip` carried no position, so a run of
containers that cannot hold keywords left the bar parked wherever the last
writable clip put it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()


def test_the_skip_event_carries_its_position():
    """Without index/total on the wire the frontend has nothing to advance on."""
    emit = CLI_PY[CLI_PY.index('_emit(\n                    "tag-skip"'):]
    emit = emit[:emit.index(")\n")]
    assert "index=idx" in emit
    assert "total=len(mapping)" in emit


def test_the_write_phase_reports_its_position():
    handler = MAIN_TS[MAIN_TS.index('case "tag-video":'):]
    handler = handler[:handler.index("break;")]
    assert "evt.index}/${evt.total}" in handler
    assert "tagEtaSuffix()" in handler


def test_a_skipped_clip_still_moves_the_bar():
    handler = MAIN_TS[MAIN_TS.index('case "tag-skip":'):]
    handler = handler[:handler.index("break;")]
    assert "batch.tagDone = evt.index" in handler
    assert "setProgress" in handler


def test_the_write_phase_times_itself_rather_than_reusing_the_scans_clock():
    """The scan's clock starts before the labeler, where the user spends an
    unbounded amount of time typing names. Reusing it would make every write
    estimate meaningless."""
    assert "tagStartedAt" in MAIN_TS
    start = MAIN_TS[MAIN_TS.index('case "tag-start":'):]
    start = start[:start.index("break;")]
    assert "batch.tagStartedAt = Date.now()" in start
    assert "batch.tagDone = 0" in start


def test_both_phases_share_one_estimator():
    """Two copies of this drift. The scan's version was already correct."""
    assert MAIN_TS.count("function eta(") == 1
    assert "return eta(batch.scanDone, batch.scanTotal, batch.startedAt)" in MAIN_TS
    assert "return eta(batch.tagDone, batch.tagTotal, batch.tagStartedAt)" in MAIN_TS


def test_an_estimate_is_withheld_until_it_means_something():
    """One sample extrapolated over 200 clips is a wild guess shown with the
    same confidence as a good one."""
    fn = MAIN_TS[MAIN_TS.index("function eta("):]
    fn = fn[:fn.index("\n}")]
    assert "done < 2" in fn
    assert "total <= done" in fn, "a finished phase must not advertise time left"


def test_a_new_batch_forgets_the_previous_ones_timings():
    """A stale clock would make the next run's first estimate absurd."""
    reset = MAIN_TS[MAIN_TS.index("batch.scanTotal = 0;"):]
    reset = reset[:reset.index("await ensureSidecarListener")]
    assert "batch.tagDone = 0" in reset
    assert "batch.tagStartedAt = 0" in reset
