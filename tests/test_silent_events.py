"""Steps that failed used to report success.

Thirteen events the sidecar emits were never declared in the frontend, so they
fell through the event switch and reached nobody. Found by generalising the
emitted-vs-declared check from `tag-*` to every event, after that check had
already caught the same class of bug twice.

Four of them were failures the user needed. `finder-error` is the one that
matters most: writing the keyword into a clip can succeed while writing its
Finder tag and Spotlight comment fails, so the run reported clean and Finder
search quietly could not find those clips by name. That is the exact defect
v0.0.98 fixed for `tag-skip`, sitting unnoticed in twelve other places.

Two were progress for the embedding backfill, which runs on any library first
scanned before activity tagging existed and showed nothing at all while it ran.

The remaining seven are console-only on purpose, and declared so that being
console-only is a decision rather than an oversight.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()

SURFACED = ["finder-error", "markers-skip", "energy-skip", "index-prune-error"]


def test_the_sidecar_still_emits_each_one():
    """If an emit disappears, the row it feeds goes quiet with nothing failing."""
    emitted = set(re.findall(r'_emit\(\s*"([a-z0-9-]+)"', CLI_PY))
    for name in SURFACED + ["activity-backfill", "activity-backfill-start"]:
        assert name in emitted, f"{name} is no longer emitted"


def test_each_failure_is_kept_rather_than_only_logged():
    for state in ("lastFinderErrors", "lastMarkerSkips", "lastEnergySkips",
                  "lastIndexPruneError"):
        assert state in MAIN_TS, f"{state} missing"
    assert "lastFinderErrors.push" in MAIN_TS
    assert "lastMarkerSkips.push" in MAIN_TS
    assert "lastEnergySkips.push" in MAIN_TS
    assert "lastIndexPruneError = evt.message" in MAIN_TS


def test_a_failed_finder_write_reaches_the_done_screen():
    """The keyword can land while this fails. Silence means the user believes
    Spotlight will find the clip and it will not."""
    row = MAIN_TS[MAIN_TS.index('label: "Finder tags that failed"'):]
    row = row[:row.index("},")]
    assert "lastFinderErrors.slice(0, 8)" in row
    assert "more`" in row, "truncation must announce itself"
    assert "bad: true" in row, "an empty row here would warn on every clean run"


def test_every_new_row_hides_itself_when_there_is_nothing_to_say():
    """These rows are failures, so empty is the good case. Without bad:true the
    panel renders "⚠ label: empty" on every successful run."""
    for label in ("Finder tags that failed", "Skipped (can't hold markers)",
                  "Not scored for energy", "Index cleanup"):
        row = MAIN_TS[MAIN_TS.index(f'label: "{label}"'):]
        assert "bad: true" in row[:row.index("},")], f"{label} lacks bad:true"


def test_a_new_run_forgets_the_previous_runs_failures():
    """Stale rows accuse clips that just wrote fine."""
    start = MAIN_TS.index("async function runWrite")
    body = MAIN_TS[start:start + 2500]
    for state in ("lastFinderErrors = []", "lastMarkerSkips = []",
                  "lastEnergySkips = []", "lastIndexPruneError = null"):
        assert state in body, f"{state} not reset"


def test_the_panel_opens_for_these_alone():
    """A run whose only problem is a Finder failure still has to show it."""
    ev = MAIN_TS[MAIN_TS.index("const hasEvidence"):]
    ev = ev[:ev.index(");")]
    for state in ("lastFinderErrors.length", "lastMarkerSkips.length",
                  "lastEnergySkips.length", "lastIndexPruneError"):
        assert state in ev, f"{state} not counted as evidence"


def test_the_backfill_pass_now_reports_progress():
    """It runs on any library scanned before activity tagging existed, and
    showed nothing at all, which on a large library is a long silence."""
    start = MAIN_TS[MAIN_TS.index('case "activity-backfill-start":'):]
    start = start[:start.index("break;")]
    assert "workingLabel.textContent" in start
    step = MAIN_TS[MAIN_TS.index('case "activity-backfill":'):]
    step = step[:step.index("break;")]
    assert "evt.index}/${evt.total}" in step
    assert "setProgress" in step


def test_the_console_only_seven_are_declared_not_forgotten():
    """Declaring them is what makes console-only a decision. Each has a comment
    above it in the union saying why."""
    for name in ("cluster-empty", "cluster-skipped", "index-pruned",
                 "person-thumbs-complete", "resolve-stale-removed",
                 "timeline-duplicate-skipped", "video-energy"):
        assert f'{{ event: "{name}"' in MAIN_TS, f"{name} not declared"
        assert f'case "{name}":' in MAIN_TS, f"{name} not handled"


def test_the_allowlist_is_empty_and_must_stay_that_way():
    src = (ROOT / "tests/test_partial_tag_failure.py").read_text()
    assert "KNOWN_UNDECLARED: set[str] = set()" in src
