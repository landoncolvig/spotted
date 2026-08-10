"""Reaching the clips past the review screen's thumbnail cap.

v0.0.103 made each shown clip individually rejectable, capped at six because
every one carries a base64 JPEG on a single emit line. That left a real hole:
a tag matching forty clips showed six, said so, and offered no way to reach the
other thirty-four short of dropping the tag from all of them — which is exactly
the all-or-nothing choice per-clip pruning existed to remove.

The fix is a second, thumbnail-free fetch. Names and scores are enough to prune
by, and they cost nothing per clip, so it works the same for six or six
hundred.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from facetag import cli as _cli
from facetag import db as _db

runner = CliRunner()

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
LIB_RS = (ROOT / "app/src-tauri/src/lib.rs").read_text()
STYLES = (ROOT / "app/src/styles.css").read_text()


def _library(tmp_path: Path, n: int = 9):
    conn = _db.connect(tmp_path / "t.db")
    for i in range(n):
        clip = tmp_path / f"c{i}.mov"
        clip.write_bytes(b"")
        vid = _db.add_video(conn, str(clip), 2.0)
        conn.execute(
            "INSERT INTO auto_tags(video_id, tag, score) VALUES (?,?,?)",
            (vid, "beach", 0.10 + i * 0.01),
        )
    # A second tag, to prove the listing is scoped to the one asked for.
    conn.execute(
        "INSERT INTO auto_tags(video_id, tag, score) "
        "VALUES ((SELECT id FROM videos LIMIT 1), 'sunset', 0.5)"
    )
    conn.commit()
    conn.close()


def _run(tmp_path: Path, tag: str = "beach", *extra: str):
    return runner.invoke(
        _cli.app,
        ["activity-clips", "--db", str(tmp_path / "t.db"), "--tag", tag, *extra],
    )


def test_it_lists_every_clip_the_tag_matched(tmp_path):
    _library(tmp_path, 9)
    out = _run(tmp_path).output
    assert '"tag": "beach"' in out or '"tag":"beach"' in out
    assert out.count('"name":') == 9, "all nine, not a capped subset"


def test_it_carries_no_thumbnails(tmp_path):
    """The whole reason this exists. A base64 image per clip is what caps the
    other payload at six."""
    _library(tmp_path, 9)
    assert "data:image" not in _run(tmp_path).output


def test_weakest_first_matches_the_review_screens_order(tmp_path):
    """The row above shows the six weakest; the rest continuing in the same
    direction is the only ordering that does not read as random."""
    _library(tmp_path, 5)
    out = _run(tmp_path).output
    names = re.findall(r'"name":\s*"([^"]+)"', out)
    assert names == ["c0.mov", "c1.mov", "c2.mov", "c3.mov", "c4.mov"]


def test_it_lists_only_the_tag_asked_for(tmp_path):
    _library(tmp_path, 3)
    assert '"sunset"' not in _run(tmp_path).output


def test_the_tag_match_is_case_insensitive(tmp_path):
    """auto_tags stores lowercased; the review screen shows what the user
    typed."""
    _library(tmp_path, 3)
    assert _run(tmp_path, "Beach").output.count('"name":') == 3


def test_an_unknown_tag_is_an_empty_list_not_an_error(tmp_path):
    _library(tmp_path, 3)
    res = _run(tmp_path, "nothing-matched-this")
    assert res.exit_code == 0
    assert '"clips": []' in res.output or '"clips":[]' in res.output


def test_the_scope_confines_it_to_the_batch(tmp_path):
    _library(tmp_path, 3)
    sub = tmp_path / "sub"
    sub.mkdir()
    assert _run(tmp_path, "beach", "--scope", str(sub)).output.count('"name":') == 0


def test_the_frontend_asks_for_the_rest_rather_than_just_mentioning_them():
    """It used to be a sentence saying the rest existed and could not be
    reached."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert 'invoke<string>("list_tag_clips"' in fn
    assert "show the rest" in fn


def test_the_already_shown_clips_are_not_listed_twice():
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert "already.has(c.path)" in fn


def test_the_expanded_rows_feed_the_same_rejection_list():
    """Two paths recording rejections differently is how one of them silently
    stops working."""
    fn = MAIN_TS[MAIN_TS.index("function buildRestOfClips"):]
    fn = fn[:fn.index("\n  return wrap;")]
    assert "droppedClips.push([c.path, tag])" in fn
    assert "droppedClips.splice(i, 1)" in fn, "unchecking must be reversible"


def test_a_failed_fetch_says_so():
    """A button that quietly does nothing reads as the rest not existing."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert "Couldn't load the rest" in fn


def test_the_row_can_hold_a_second_line():
    """The list is flex-basis 100% inside .review-row, which was a
    non-wrapping flex line — without wrap it is squeezed onto the same row
    instead of dropping below."""
    row = STYLES[STYLES.index(".review-row {"):]
    assert "flex-wrap: wrap" in row[:row.index("}")]


def test_the_expanded_list_scrolls_rather_than_growing_without_bound():
    block = STYLES[STYLES.index(".review-rest {"):]
    block = block[:block.index("}")]
    assert "max-height" in block and "overflow-y: auto" in block


def test_both_capture_callers_share_one_implementation():
    """suggest_activities and list_tag_clips both need the stdout back. Two
    copies of that 60-line spawn-and-collect drift."""
    assert LIB_RS.count("async fn run_sidecar_capture") == 1
    assert LIB_RS.count("run_sidecar_capture(app, window, args).await") == 2
