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


def test_the_finish_line_reports_the_batch_not_the_library():
    """A tester dropped one clip and was told Spotted had tagged 169. The
    finish line was calling `status` unscoped and reading library totals."""
    source = _run_write_source()
    assert "fetchBatchStats()" in source, "the Done screen is back on library totals"
    assert source.index("fetchBatchStats()") < source.index("setState(\"done\")")


def test_retag_library_still_reports_the_library():
    """Re-tag Library never scans, so the recorded batch is whatever was
    dropped last. Scoping its finish line to that headlines "1 clip" over a
    coverage row reading "169 of 169"."""
    source = _run_write_source()
    stats = source[source.index("const stats ="):source.index("setState(\"done\")")]
    assert "allClips" in stats, "Re-tag Library is reading the last drop's numbers"
    assert "fetchLibraryStats()" in stats


def test_batch_stats_never_reach_the_library_view():
    """batch-stats carries one drop's numbers. Forwarding it to the Library
    view would render that drop as the user's entire library."""
    source = (ROOT / "app/src/main.ts").read_text()
    start = source.index('case "batch-stats":')
    end = source.index("break;", start)
    # Comments may name the function they are warning about; only the code
    # counts here.
    body = "\n".join(
        l for l in source[start:end].splitlines() if not l.strip().startswith("//")
    )
    assert "handleLibraryEvent" not in body
    assert "lastBatchStats = evt" in body
