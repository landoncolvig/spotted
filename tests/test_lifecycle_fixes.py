"""Tests for the v0.0.49 correctness + security fixes:
scan-complete flag, marker de-duplication, labeler XSS escaping, and the
faceless-folder cluster exit code."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from facetag import cli as _cli
from facetag import db as _db
from facetag import markers as _markers
from facetag.web import _render_seen_in


# --- scan-complete flag drives is_scanned -----------------------------------
def test_faces_alone_do_not_count_as_scanned(tmp_path):
    conn = _db.connect(tmp_path / "i.db")
    vid = _db.add_video(conn, "/clip.mov", 5.0)
    # A row exists but the clip never finished — must NOT read as scanned
    # (the old "has a face row" sentinel is what caused permanent skips).
    assert _db.is_scanned(conn, "/clip.mov") is False
    _db.mark_scan_complete(conn, vid)
    assert _db.is_scanned(conn, "/clip.mov") is True


def test_migration_backfills_flag_for_existing_face_clips(tmp_path):
    # Build a pre-scan_complete DB (SCHEMA has no such column) with a clip that
    # already has a face, then open it through db.connect (which migrates).
    p = tmp_path / "legacy.db"
    raw = sqlite3.connect(p)
    raw.executescript(_db.SCHEMA)
    raw.execute("INSERT INTO videos(path, duration_sec) VALUES('/old.mov', 4)")
    vid = raw.execute("SELECT id FROM videos WHERE path='/old.mov'").fetchone()[0]
    raw.execute(
        "INSERT INTO faces(video_id, timestamp_sec, embedding) VALUES(?, 0.0, X'00')",
        (vid,),
    )
    raw.commit()
    raw.close()

    conn = _db.connect(p)  # runs _migrate → backfills scan_complete
    # Existing scanned libraries must not be force-rescanned.
    assert _db.is_scanned(conn, "/old.mov") is True


def test_migration_does_not_flag_faceless_legacy_clip(tmp_path):
    p = tmp_path / "legacy2.db"
    raw = sqlite3.connect(p)
    raw.executescript(_db.SCHEMA)
    raw.execute("INSERT INTO videos(path, duration_sec) VALUES('/broll.mov', 4)")
    raw.commit()
    raw.close()
    conn = _db.connect(p)
    # No face row → not backfilled → will be (re)scanned once so its flag gets set.
    assert _db.is_scanned(conn, "/broll.mov") is False


# --- markers no longer duplicate on re-run ----------------------------------
def test_markers_do_not_duplicate_on_rerun(test_mov, have_exiftool):
    if not have_exiftool:
        import pytest
        pytest.skip("exiftool not on PATH")
    events = [(0.5, "Sarah"), (1.0, "Dad")]
    _markers.write_markers(test_mov, events)
    _markers.write_markers(test_mov, events)   # second run must replace, not append
    raw = _markers.read_markers(test_mov)
    assert raw.count("Sarah") == 1
    assert raw.count("Dad") == 1


# --- labeler escapes attacker-controlled filenames --------------------------
def test_seen_in_escapes_malicious_filename():
    out = _render_seen_in(["/footage/<img src=x onerror=alert(1)>.mov"])
    assert "<img" not in out          # not rendered as a live tag
    assert "&lt;img" in out           # escaped instead
    assert "onerror=alert(1)" not in out or "&lt;img" in out


# --- sidecar auto-cleanup keeps folders tidy, never eats a foreign sidecar --
def test_sidecar_cleanup_removes_only_spotted(test_mov, have_exiftool):
    import shutil
    import subprocess

    if not have_exiftool:
        import pytest
        pytest.skip("exiftool not on PATH")
    exe = shutil.which("exiftool")
    sc = _markers.sidecar_path_for(test_mov)

    # Spotted's own sidecar gets cleaned up.
    _markers.write_markers_sidecar(test_mov, [(0.5, "Sarah")])
    assert sc.exists()
    assert _markers.delete_sidecar_if_spotted(test_mov) is True
    assert not sc.exists()

    # A foreign sidecar (another tool's CreatorTool) is preserved.
    subprocess.run([exe, "-q", "-o", str(sc), "-XMP-xmp:CreatorTool=DaVinci"],
                   check=True, capture_output=True)
    assert sc.exists()
    assert _markers.delete_sidecar_if_spotted(test_mov) is False
    assert sc.exists()

    # No sidecar → no-op.
    sc.unlink()
    assert _markers.delete_sidecar_if_spotted(test_mov) is False


# --- DaVinci Resolve marker script generation -------------------------------
def test_resolve_marker_script_is_valid_python_with_data():
    vm = {
        "/x/A.mov": [(0.5, "Sarah"), (1.0, "Energy peak")],
        "/x/B.mov": [(2.0, "Dad")],
    }
    script = _markers.resolve_marker_script(vm)
    assert script
    compile(script, "Spotted Markers.py", "exec")   # must be runnable Python
    for token in ("A.mov", "B.mov", "Sarah", "Dad", "Energy peak", "AddMarker", "Yellow", "Blue"):
        assert token in script
    assert _markers.resolve_marker_script({}) == ""   # nothing to mark → empty


def test_write_resolve_script_lands_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))         # isolate from the real ~/Library
    vm = {str(tmp_path / "clip.mov"): [(0.5, "Sarah")]}
    out = _markers.write_resolve_script(vm, tmp_path)
    assert out is not None and out.exists() and out.name == "Spotted Markers.py"
    assert "Sarah" in out.read_text()
    assert _markers.write_resolve_script({}, tmp_path) is None


# --- faceless folder no longer hard-errors ----------------------------------
def test_cluster_with_no_faces_exits_zero(tmp_path):
    db_path = tmp_path / "empty.db"
    _db.connect(db_path).close()      # valid, empty index — no faces
    res = CliRunner().invoke(_cli.app, ["cluster", "--db", str(db_path)])
    assert res.exit_code == 0, res.output   # was Exit(1) before; must not block the flow
