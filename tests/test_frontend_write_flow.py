"""Regression checks for the frontend's serial write orchestration.

The browser code has no unit-test runtime, so these checks pin the two
structural invariants that protect partial tag failures. TypeScript compilation
and the visible-UI check exercise the rendered behavior separately.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run_write_source() -> str:
    source = (ROOT / "app/src/main.ts").read_text()
    start = source.index("async function runWrite")
    end = source.index("\nfunction summarizeDone", start)
    return source[start:end]


def test_marker_export_runs_after_a_tag_write_failure():
    source = _run_write_source()

    tag_call = source.index('invoke<number>("tag_videos"')
    caught_error = source.index("tagWriteError = String(e)")
    marker_call = source.index('invoke<number>("write_markers"')
    partial_result = source.index("if (tagWriteError)")

    assert tag_call < caught_error < marker_call < partial_result


def test_new_write_clears_previous_export_evidence():
    source = _run_write_source()

    assert "lastMarkersSummary = null" in source
    assert 'document.getElementById("done-verify")?.replaceChildren()' in source
