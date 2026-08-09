"""A CSV of what Spotted put in each clip, that the user keeps.

The Done screen is the only record of a run and it disappears on the next
drop. This is the artifact someone can open later to check the work, hand to a
colleague, or diff against what they expected.

It reports `videos.spotted_keywords` — the set Spotted last actually WROTE into
that file — not the set it intended to write. Those differ whenever a clip
failed or its container could not hold keywords, and a report that quietly
showed intent would be worse than no report at all, because its whole purpose
is being checkable after the fact.
"""

from __future__ import annotations

import csv
from pathlib import Path

from typer.testing import CliRunner

from facetag import cli as _cli
from facetag import db as _db

runner = CliRunner()


def _library(tmp_path: Path):
    conn = _db.connect(tmp_path / "t.db")
    made = {}
    for name in ("kept.mov", "gone.mov"):
        clip = tmp_path / name
        clip.write_bytes(b"")
        made[name] = _db.add_video(conn, str(clip), 2.0)
    conn.execute("INSERT INTO people(cluster_id,name) VALUES(3,'Ellie')")
    conn.execute(
        "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
        "VALUES(?,?,X'00',3)", (made["kept.mov"], 0.0),
    )
    conn.execute(
        "INSERT INTO auto_tags(video_id, tag, score) VALUES (?,?,?)",
        (made["kept.mov"], "beach", 0.4),
    )
    conn.execute(
        "UPDATE videos SET energy_bucket='high', energy_peaks='[1.0, 2.0]' WHERE id=?",
        (made["kept.mov"],),
    )
    _db.set_spotted_keywords(conn, made["kept.mov"], ["Ellie", "beach", "high energy"])
    conn.commit()
    conn.close()
    return made


def _run(tmp_path: Path, *extra: str):
    out = tmp_path / "r.csv"
    res = runner.invoke(
        _cli.app,
        ["report", "--db", str(tmp_path / "t.db"), "--out", str(out), *extra],
    )
    assert res.exit_code == 0, res.output
    with out.open() as fh:
        return list(csv.DictReader(fh))


def test_it_reports_what_was_written_not_what_was_intended(tmp_path):
    _library(tmp_path)
    rows = {r["clip"]: r for r in _run(tmp_path)}
    assert rows["kept.mov"]["keywords written"] == "Ellie; beach; high energy"
    # gone.mov was indexed but never written, and must not claim otherwise.
    assert rows["gone.mov"]["keywords written"] == ""


def test_it_carries_the_detail_needed_to_check_the_work(tmp_path):
    _library(tmp_path)
    row = {r["clip"]: r for r in _run(tmp_path)}["kept.mov"]
    assert row["people"] == "Ellie"
    assert row["tags"] == "beach"
    assert row["energy"] == "high"
    assert row["energy peaks"] == "2"
    assert row["folder"] == str(tmp_path)


def test_a_clip_that_left_the_disk_is_marked_rather_than_dropped(tmp_path):
    """Silently omitting it would make the report disagree with the library
    for a reason the reader cannot see."""
    _library(tmp_path)
    (tmp_path / "gone.mov").unlink()
    rows = {r["clip"]: r for r in _run(tmp_path)}
    assert rows["kept.mov"]["on disk"] == "yes"
    assert rows["gone.mov"]["on disk"] == "no"


def test_the_scope_confines_it_to_one_batch(tmp_path):
    """A report headed by a Done screen that said "1 clip" must not quietly
    contain the whole library."""
    _library(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    only = sub / "only.mov"
    only.write_bytes(b"")
    conn = _db.connect(tmp_path / "t.db")
    _db.add_video(conn, str(only), 1.0)
    conn.commit()
    conn.close()
    names = {r["clip"] for r in _run(tmp_path, "--scope", str(sub))}
    assert names == {"only.mov"}


def test_commas_in_a_name_survive_the_csv(tmp_path):
    """Person names and filenames are user data. The keyword column joins on
    "; " precisely because a comma is legal inside one."""
    made = _library(tmp_path)
    conn = _db.connect(tmp_path / "t.db")
    conn.execute("UPDATE people SET name='Smith, Ellie' WHERE cluster_id=3")
    _db.set_spotted_keywords(conn, made["kept.mov"], ["Smith, Ellie"])
    conn.commit()
    conn.close()
    row = {r["clip"]: r for r in _run(tmp_path)}["kept.mov"]
    assert row["people"] == "Smith, Ellie"
    assert row["keywords written"] == "Smith, Ellie"


def test_it_writes_a_header_even_for_an_empty_library(tmp_path):
    """An empty file gives the user nothing to tell "no clips" apart from
    "the export broke"."""
    _db.connect(tmp_path / "t.db").close()
    out = tmp_path / "empty.csv"
    res = runner.invoke(
        _cli.app, ["report", "--db", str(tmp_path / "t.db"), "--out", str(out)]
    )
    assert res.exit_code == 0
    assert out.read_text().startswith("clip,folder,people")


def test_the_button_reports_the_batch_the_done_screen_described(tmp_path):
    """Re-tag Library is the one run that really is library-wide; every other
    run must not widen past its own batch."""
    main_ts = (Path(__file__).resolve().parent.parent / "app/src/main.ts").read_text()
    handler = main_ts[main_ts.index('btnReport?.addEventListener'):]
    handler = handler[:handler.index("\n});")]
    assert "lastRunWasLibraryWide ? null : (currentPath ?? null)" in handler
    assert "allClips: lastRunWasLibraryWide" in handler


def test_a_failed_export_says_so(tmp_path):
    """The user asked for a file and would otherwise go looking for one that
    is not there."""
    main_ts = (Path(__file__).resolve().parent.parent / "app/src/main.ts").read_text()
    handler = main_ts[main_ts.index('btnReport?.addEventListener'):]
    handler = handler[:handler.index("\n});")]
    assert "showError(String(e))" in handler
    assert "if (!target) return" in handler, "cancelling the dialog is not an error"
