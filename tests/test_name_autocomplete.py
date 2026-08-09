"""One person, one name, however it was typed.

Clusters were consolidated by grouping on the raw name string, so "Grayson"
and "grayson" were two different people — permanently. The labeler kept showing
two cards for them, and an editor searching one spelling missed half the clips.
A single inconsistent keystroke did that, with nothing on screen to suggest
anything had gone wrong.

Two halves. The labeler now suggests names already in the library, so the
second spelling is less likely to be typed. And the merge matches
case-insensitively, so it stops mattering when someone types it anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from facetag import db as _db
from facetag import web as _web

ROOT = Path(__file__).resolve().parent.parent


def _library(tmp_path: Path, *named: tuple[int, str, int]):
    """(cluster_id, name, face_count) per person."""
    conn = _db.connect(tmp_path / "t.db")
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"")
    vid = _db.add_video(conn, str(clip), 2.0)
    for cid, name, faces in named:
        conn.execute("INSERT INTO people(cluster_id,name) VALUES(?,?)", (cid, name))
        for i in range(faces):
            conn.execute(
                "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
                "VALUES(?,?,X'00',?)", (vid, float(i), cid),
            )
    conn.commit()
    return conn


def test_different_capitalisation_is_one_person(tmp_path):
    conn = _library(tmp_path, (1, "Grayson", 5), (2, "grayson", 2))
    _db.merge_clusters_by_name(conn)
    people = conn.execute("SELECT cluster_id, name FROM people").fetchall()
    assert len(people) == 1, f"expected one Grayson, got {people}"
    assert conn.execute(
        "SELECT COUNT(*) FROM faces WHERE cluster_id = 1"
    ).fetchone()[0] == 7, "the smaller cluster's faces should have moved over"


def test_stray_whitespace_is_one_person(tmp_path):
    conn = _library(tmp_path, (1, "Ellie", 4), (2, "Ellie ", 1))
    _db.merge_clusters_by_name(conn)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


def test_the_surviving_spelling_is_the_one_most_faces_were_filed_under(tmp_path):
    """The merge is invisible unless it settles the spelling too: the name in
    the file is the only place the user ever sees it."""
    conn = _library(tmp_path, (1, "grayson", 2), (2, "Grayson", 9))
    _db.merge_clusters_by_name(conn)
    assert conn.execute("SELECT name FROM people").fetchone()[0] == "Grayson"


def test_genuinely_different_names_are_left_alone(tmp_path):
    conn = _library(tmp_path, (1, "Ellie", 3), (2, "Grayson", 3))
    _db.merge_clusters_by_name(conn)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 2


def test_merging_twice_changes_nothing_the_second_time(tmp_path):
    conn = _library(tmp_path, (1, "Grayson", 5), (2, "grayson", 2))
    _db.merge_clusters_by_name(conn)
    before = conn.execute("SELECT cluster_id, name FROM people").fetchall()
    assert _db.merge_clusters_by_name(conn) == {}
    assert conn.execute("SELECT cluster_id, name FROM people").fetchall() == before


def test_the_suggestion_list_cannot_itself_offer_both_spellings(tmp_path):
    """Suggesting "Grayson" and "grayson" side by side would hand the user the
    exact mistake this is meant to prevent."""
    conn = _library(tmp_path, (1, "Grayson", 5), (2, "grayson", 2), (3, "", 1))
    names = _db.known_names(conn)
    assert names == ["Grayson"]


@pytest.fixture
def client(tmp_path):
    conn = _library(tmp_path, (1, "Ellie", 4))
    conn.close()
    app = _web.create_app(tmp_path / "t.db", tmp_path / "thumbs")
    _web._install_guard(app, "TOK")
    return app.test_client()


def test_the_labeler_offers_the_names_already_in_the_library(client):
    body = client.get("/?k=TOK&view=all").get_data(as_text=True)
    assert '<datalist id="known-names">' in body
    assert '<option value="Ellie">' in body
    assert 'list="known-names"' in body, "the input is not wired to the list"


def test_the_suggestions_survive_the_pages_csp(client):
    """A datalist was chosen over a JS autocomplete because the labeler runs
    under a nonce CSP with no inline handlers. If this ever becomes script,
    it needs the nonce."""
    resp = client.get("/?k=TOK&view=all")
    body = resp.get_data(as_text=True)
    assert body.count("<script") == 1, "a second script would need its own nonce"
    assert re.search(r'<datalist[^>]*>(<option[^>]*>)+</datalist>', body)


def test_a_name_containing_markup_is_inert_in_the_suggestion(tmp_path):
    conn = _library(tmp_path, (1, '"><script>alert(1)</script>', 3))
    conn.close()
    app = _web.create_app(tmp_path / "t.db", tmp_path / "thumbs")
    _web._install_guard(app, "T")
    body = app.test_client().get("/?k=T&view=all").get_data(as_text=True)
    assert "<script>alert(1)" not in body
