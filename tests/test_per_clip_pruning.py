"""Dropping a tag from one clip, not from every clip it found.

The review screen could only reject a whole tag. Unchecking "beach" said
"beach was never right"; there was no way to say "beach is right, just not in
this one". A user with one bad match had two options: keep the bad tag, or lose
the tag everywhere it was correct.

The selection rule is the part worth guarding. The thumbnails shown are the
tag's WEAKEST matches, not its strongest. Showing the top scorers is right for
a preview and wrong for pruning: the strongest matches are the ones most likely
correct, and a user opening this row is looking for the ones to remove.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from facetag import db as _db

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
LIB_RS = (ROOT / "app/src-tauri/src/lib.rs").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()


def _tagged(tmp_path: Path):
    conn = _db.connect(tmp_path / "t.db")
    made = {}
    for name in ("a.mov", "b.mov"):
        clip = tmp_path / name
        clip.write_bytes(b"")
        vid = _db.add_video(conn, str(clip), 2.0)
        made[name] = (vid, str(clip))
        for tag in ("beach", "sunset"):
            conn.execute(
                "INSERT INTO auto_tags(video_id, tag, score) VALUES (?,?,?)",
                (vid, tag, 0.4),
            )
    conn.commit()
    return conn, made


def test_one_clip_loses_the_tag_and_the_others_keep_it(tmp_path):
    conn, made = _tagged(tmp_path)
    removed = _db.delete_auto_tag_pairs(conn, [(made["a.mov"][1], "beach")])
    assert removed == 1
    rows = conn.execute(
        "SELECT video_id, tag FROM auto_tags ORDER BY video_id, tag"
    ).fetchall()
    assert (made["a.mov"][0], "beach") not in rows
    assert (made["a.mov"][0], "sunset") in rows, "the clip's other tags stay"
    assert (made["b.mov"][0], "beach") in rows, "the tag stays on other clips"


def test_the_rejection_is_case_insensitive_like_the_tag_level_one(tmp_path):
    conn, made = _tagged(tmp_path)
    assert _db.delete_auto_tag_pairs(conn, [(made["a.mov"][1], "Beach")]) == 1


def test_an_unknown_clip_or_empty_pair_is_a_no_op(tmp_path):
    conn, made = _tagged(tmp_path)
    assert _db.delete_auto_tag_pairs(conn, []) == 0
    assert _db.delete_auto_tag_pairs(conn, [("/nope.mov", "beach")]) == 0
    assert _db.delete_auto_tag_pairs(conn, [(made["a.mov"][1], "  ")]) == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_tags").fetchone()[0] == 4


def test_the_rejection_outlives_this_write(tmp_path):
    """It is a DELETE, not a filter. A tag that came back on the next write
    would make the pruning look broken."""
    conn, made = _tagged(tmp_path)
    _db.delete_auto_tag_pairs(conn, [(made["a.mov"][1], "beach")])
    assert _db.get_auto_tags(conn, made["a.mov"][0]) == [("sunset", 0.4)]


def test_the_clips_offered_for_pruning_are_the_weakest_matches():
    """Not the strongest. See this module's docstring."""
    agg = CLI_PY[CLI_PY.index("def _aggregate"):]
    agg = agg[:agg.index("return sorted(rollup")]
    assert "ordered = sorted(hits, key=lambda x: -x[1])" in agg
    assert "ordered[-CLIPS_SHOWN_PER_TAG:]" in agg, "showing the top scorers"


def test_each_shown_clip_carries_what_pruning_needs():
    agg = CLI_PY[CLI_PY.index("def _aggregate"):]
    agg = agg[:agg.index("return sorted(rollup")]
    for field in ('"path"', '"name"', '"score"', '"thumb"'):
        assert field in agg, f"shown clips are missing {field}"


def test_the_cap_announces_itself():
    """Someone who prunes the six shown and sees the tag land on 40 clips
    would reasonably conclude the pruning does not work."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert "m.clips > m.shown.length" in fn
    assert "weakest of" in fn


def test_a_clip_with_no_thumbnail_is_still_prunable():
    """Losing the preview must not also lose the control."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert "review-thumb--missing" in fn
    block = fn[fn.index("if (clip.thumb)"):fn.index("cell.addEventListener")]
    assert "else" in block


def test_rejections_travel_as_a_file_not_as_argv():
    """The values are filesystem paths. There is no separator they cannot
    legally contain, so any delimited argv encoding is guessing."""
    assert "--drop-pairs-file" in LIB_RS
    assert "serde_json::to_string(&pairs)" in LIB_RS
    assert "--drop-pairs-file" in CLI_PY


def test_a_malformed_rejection_list_does_not_take_the_write_with_it():
    """Losing the pruning is recoverable. Losing the tagging is not."""
    body = CLI_PY[CLI_PY.index("drop_pairs_file and drop_pairs_file.exists()"):]
    body = body[:body.index("mapping = _tag.videos_with_keywords")]
    assert "except (ValueError, TypeError)" in body
    assert "tag-prune-error" in body


def test_the_pruning_happens_before_the_mapping_is_built():
    """A clip pruned after the mapping was computed would still be written."""
    prune = CLI_PY.index("delete_auto_tag_pairs")
    build = CLI_PY.index("mapping = _tag.videos_with_keywords")
    assert prune < build


def test_a_rerendered_review_forgets_stale_rejections():
    """Re-rendering rebuilds every control, so rejections recorded against the
    old DOM point at buttons that no longer exist."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    assert "droppedClips = [];" in fn[:400]


def test_the_rejections_reach_the_write():
    call = re.search(r'invoke<number>\("tag_videos", \{.*?\}\)', MAIN_TS, re.S)
    assert call and "dropPairs: droppedClips" in call.group(0)


def test_the_json_shape_matches_what_python_unpacks():
    """Rust writes pairs; Python unpacks `for p, t in pairs`. A shape change on
    either side is silent until a user prunes something."""
    assert "for p, t in pairs" in CLI_PY
    sample = json.dumps([["/a.mov", "beach"]])
    assert [tuple(x) for x in json.loads(sample)] == [("/a.mov", "beach")]


def test_a_failed_prune_is_reported_rather_than_looking_like_success():
    """The user unchecked clips, the write went ahead, and those tags are in
    their files. Silence here reads as it having worked."""
    assert "lastPruneError" in MAIN_TS
    handler = MAIN_TS[MAIN_TS.index('case "tag-prune-error":'):]
    assert "lastPruneError = evt.message" in handler[:200]
    panel = MAIN_TS[MAIN_TS.index('label: "Clips you unchecked"'):]
    assert "not applied" in panel[:400]
    assert "bad: true" in panel[:500], "an empty row here must not render"
