"""Unit tests for the keyword-merge behavior that changed when tagging moved
from a fixed prompt list to the user's own tags, matched per clip.

The contract these lock in:
  1. The tags a user types are NOT stamped onto every clip. They are the
     matcher's input vocabulary; they only reach a file if the matcher wrote
     them into auto_tags for that specific clip (same as a face name).
  2. Matched (auto) tags are per-clip.
  3. The review screen's unchecked tags (exclude_tags) are left out of the write.

Pure SQLite — no MobileCLIP model, ffmpeg, or exiftool needed.
"""
from __future__ import annotations

from facetag import db as _db
from facetag import tag as _tag


def _conn(tmp_path):
    return _db.connect(tmp_path / "i.db")


def test_all_batch_tags_unions_distinct(tmp_path) -> None:
    conn = _conn(tmp_path)
    a = _db.add_video(conn, "/a.mov", 1.0)
    b = _db.add_video(conn, "/b.mov", 1.0)
    _db.set_batch_tags(conn, a, ["Beach", "kids"])
    _db.set_batch_tags(conn, b, ["kids", "dog"])
    assert _db.all_batch_tags(conn) == ["beach", "dog", "kids"]


def test_no_batch_tags_returns_empty(tmp_path) -> None:
    conn = _conn(tmp_path)
    _db.add_video(conn, "/a.mov", 1.0)
    assert _db.all_batch_tags(conn) == []


def test_batch_tags_are_not_stamped_on_every_clip(tmp_path) -> None:
    """A clip that has the user's tags in batch_tags but no matched tag and no
    named face must NOT get keywords. Before the change, batch_tags were
    stamped onto every clip; that's exactly what Ellie flagged."""
    conn = _conn(tmp_path)
    vid = _db.add_video(conn, "/a.mov", 1.0)
    _db.set_batch_tags(conn, vid, ["beach", "wedding"])
    # No auto_tags, no named faces.
    assert _tag.videos_with_keywords(conn) == {}


def test_matched_tags_are_per_clip(tmp_path) -> None:
    conn = _conn(tmp_path)
    a = _db.add_video(conn, "/a.mov", 1.0)
    b = _db.add_video(conn, "/b.mov", 1.0)
    # "beach" was found in clip A only.
    _db.set_batch_tags(conn, a, ["beach"])
    _db.set_batch_tags(conn, b, ["beach"])
    _db.replace_auto_tags(conn, a, [("beach", 0.42)])
    mapping = _tag.videos_with_keywords(conn)
    assert mapping == {"/a.mov": ["beach"]}
    assert "/b.mov" not in mapping


def test_exclude_tags_drops_unchecked_matches(tmp_path) -> None:
    conn = _conn(tmp_path)
    a = _db.add_video(conn, "/a.mov", 1.0)
    _db.replace_auto_tags(conn, a, [("beach", 0.42), ("wedding", 0.11)])
    # User unchecked "wedding" on the review screen.
    mapping = _tag.videos_with_keywords(conn, exclude_tags={"wedding"})
    assert mapping == {"/a.mov": ["beach"]}


def test_exclude_tags_is_case_insensitive(tmp_path) -> None:
    conn = _conn(tmp_path)
    a = _db.add_video(conn, "/a.mov", 1.0)
    _db.replace_auto_tags(conn, a, [("Wedding", 0.11)])
    mapping = _tag.videos_with_keywords(conn, exclude_tags={"wedding"})
    assert mapping == {}
