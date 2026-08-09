"""A batch where some clips tagged and some did not must say so.

`tag-write` exits non-zero if any single clip fails, so the frontend received
one error string for the whole batch and rendered "Couldn't finish". One
unwritable clip out of 107 read exactly the same as all 107 failing, which
sends someone back to re-run a batch that was almost entirely fine.

The per-clip detail was already on the wire — the sidecar emits `tag-error`
per clip and a `tag-failed` tally — and the frontend was throwing it away in a
console.warn. These tests pin that it is kept and reported.

The browser code has no unit-test runtime, so this reads the source, the same
way test_frontend_write_flow.py does.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()


# Tolerant of line breaks inside the call. Anchoring on `_emit("tag-skip"`
# meant that wrapping the call across lines — which says nothing about
# behaviour — reported itself as the sidecar having stopped emitting the event.
EMITTED = set(re.findall(r'_emit\(\s*"(tag-[a-z-]+)"', CLI_PY))


def test_the_sidecar_still_emits_what_this_feature_reads():
    """If the sidecar stops sending these, the done screen goes quiet again
    and no frontend test would notice."""
    assert {"tag-error", "tag-skip", "tag-failed"} <= EMITTED
    assert "failed=len(failed), total=len(mapping)" in CLI_PY


def test_every_emitted_tag_event_is_declared_in_the_frontend():
    """tag-skip was emitted by the sidecar and absent from the TS union, so it
    fell through the event switch and was dropped without a trace."""
    declared = set(re.findall(r'\{ event: "(tag-[a-z-]+)"', MAIN_TS))
    assert EMITTED, "the emit scan matched nothing; this check would be vacuous"
    assert EMITTED <= declared, f"sidecar emits undeclared: {EMITTED - declared}"


def test_per_clip_failures_are_kept_not_just_logged():
    assert "lastTagFailures.push" in MAIN_TS
    assert "lastTagSkips.push" in MAIN_TS
    assert "lastTagFailed = evt" in MAIN_TS


def test_a_new_run_clears_the_previous_runs_failures():
    """Stale failures on a clean run are worse than none: they accuse clips
    that just tagged fine."""
    start = MAIN_TS.index("async function runWrite")
    body = MAIN_TS[start:start + 2000]
    assert "lastTagFailures = []" in body
    assert "lastTagFailed = null" in body
    assert "lastTagSkips = []" in body


def test_the_partial_line_trusts_the_sidecars_tally_not_the_event_count():
    """Counting tag-error events understates the damage when the sidecar dies
    partway, and overstating success is the direction that actually hurts."""
    fn = MAIN_TS[MAIN_TS.index("function tagWriteWasPartial"):]
    fn = fn[:fn.index("\n}")]
    assert "lastTagFailed" in fn
    assert "lastTagFailures" not in fn, "partial count must not come from the event list"


def test_partial_is_only_claimed_when_some_clips_actually_survived():
    """failed == total is a total failure and must keep saying so."""
    fn = MAIN_TS[MAIN_TS.index("function tagWriteWasPartial"):]
    fn = fn[:fn.index("\n}")]
    assert "f.failed >= f.total" in fn
    assert "f.failed <= 0" in fn
    assert "f.total <= 0" in fn


def test_a_partial_batch_stops_reading_as_couldnt_finish():
    start = MAIN_TS.index("if (tagWriteError)")
    body = MAIN_TS[start:start + 1200]
    assert "tagWriteWasPartial()" in body
    assert 'doneTitle.textContent = "Finished with issues"' in body
    # The headline changes on partial alone, not only when DaVinci files exist.
    assert "partial || lastResolveTimeline" in body


def test_a_clean_run_renders_no_failure_row():
    """The panel reads empty rows as warnings, which is inverted for a row
    that lists failures — empty is the good case there."""
    assert "if (row.bad && !filled) continue;" in MAIN_TS


def test_failed_clips_are_named_and_the_cap_is_disclosed():
    """"3 clips failed" leaves someone guessing which three."""
    start = MAIN_TS.index('label: "Clips that failed"')
    row = MAIN_TS[start:start + 500]
    assert "lastTagFailures.slice(0, 8)" in row
    assert "more`" in row, "truncation must announce itself"
    assert ".map((t) => t.name)" in row
