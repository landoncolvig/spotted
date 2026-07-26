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


# --- FCPXML timeline export (the DaVinci path that needs no scripting) -------
def test_fcpxml_is_well_formed_and_carries_markers(tmp_path):
    import xml.dom.minidom as _md

    clip = tmp_path / "a.mov"
    clip.write_bytes(b"")
    vm = {str(clip): [(0.5, "Sarah"), (1.0, "Energy peak")]}
    xml = _markers.fcpxml_for_markers(vm)
    assert xml
    _md.parseString(xml)                      # must be parseable XML
    assert "<fcpxml" in xml and "asset-clip" in xml
    for token in ("Sarah", "Energy peak", "<marker"):
        assert token in xml
    assert _markers.fcpxml_for_markers({}) == ""       # nothing to mark
    assert _markers.fcpxml_for_markers({str(clip): []}) == ""


def test_fcpxml_escapes_xml_and_snaps_to_frame_grid(tmp_path):
    clip = tmp_path / "b.mov"
    clip.write_bytes(b"")
    vm = {str(clip): [(1.0, 'Mom & Dad <"x">')]}
    xml = _markers.fcpxml_for_markers(vm)
    assert "&amp;" in xml and "&lt;" in xml and "&quot;" in xml
    assert 'Mom & Dad' not in xml                # raw ampersand would break import
    # every time value must be a rational on the timebase, never a bare float
    import re as _re
    for t in _re.findall(r'(?:start|duration|offset)="([^"]+)"', xml):
        assert t == "0s" or _re.fullmatch(r"\d+/\d+s", t), t


def test_write_fcpxml_lands_next_to_footage(tmp_path):
    clip = tmp_path / "c.mov"
    clip.write_bytes(b"")
    out = _markers.write_fcpxml({str(clip): [(0.5, "Sarah")]}, tmp_path)
    assert out is not None and out.exists()
    assert out.name == "Spotted Markers.fcpxml"
    assert _markers.write_fcpxml({}, tmp_path) is None


# --- EDL markers (the DaVinci path verified against Resolve 21) --------------
def test_edl_timecodes_account_for_clip_offsets(tmp_path):
    a = tmp_path / "a.mov"; a.write_bytes(b"")
    b = tmp_path / "b.mov"; b.write_bytes(b"")
    # No real media, so each clip falls back to (last marker + 1s) long.
    vm = {str(a): [(1.0, "Ellie")], str(b): [(1.0, "Energy peak")]}
    edl = _markers.edl_for_markers(vm)
    assert "TITLE: Spotted Markers" in edl and "FCM: NON-DROP FRAME" in edl
    assert "|M:Ellie" in edl and "|M:Energy peak" in edl
    # a.mov is 2s long, so b.mov's marker sits at 2s + 1s = 3s, not 1s.
    assert "00:00:01:00" in edl      # Ellie on the first clip
    assert "00:00:03:00" in edl      # Energy peak offset onto the second
    # energy peaks are yellow, people blue
    assert "ResolveColorYellow |M:Energy peak" in edl
    assert "ResolveColorBlue |M:Ellie" in edl
    assert _markers.edl_for_markers({}) == ""


def test_edl_marker_names_cannot_break_the_pipe_format(tmp_path):
    clip = tmp_path / "c.mov"; clip.write_bytes(b"")
    edl = _markers.edl_for_markers({str(clip): [(0.5, "Mom |D:9 |M:evil")]})
    # a raw pipe would inject extra EDL fields and corrupt the marker
    body = [l for l in edl.splitlines() if l.startswith(" |C:")][0]
    assert body.count("|M:") == 1 and body.count("|D:") == 1


def test_write_edl_lands_next_to_footage(tmp_path):
    clip = tmp_path / "d.mov"; clip.write_bytes(b"")
    out = _markers.write_edl({str(clip): [(0.5, "Sarah")]}, tmp_path)
    assert out is not None and out.exists() and out.name == "Spotted Markers.edl"
    assert _markers.write_edl({}, tmp_path) is None


# --- clips must not overlap, and timecode must be honoured -------------------
def test_layout_never_overlaps_on_the_frame_grid(tmp_path):
    """Resolve logs 'Trimming item on V1 because it overlaps previous items'
    and mangles the edit if two clips share a frame. Offsets accumulate in
    whole frames, so end(n) must equal start(n+1) exactly."""
    clips = []
    for i in range(6):
        c = tmp_path / f"c{i}.mov"; c.write_bytes(b"")
        clips.append(c)
    # durations fall back to last-marker+1s; use awkward values that would
    # drift if offsets were accumulated as floats
    vm = {str(c): [(0.1 * (i + 1), f"P{i}")] for i, c in enumerate(clips)}
    num, den, entries = _markers._timeline_layout(vm)
    spf = num / den
    for (_p, off, dur, _e), (_p2, nxt, _d2, _e2) in zip(entries, entries[1:]):
        end_frame = round((off + dur) / spf)
        next_frame = round(nxt / spf)
        assert end_frame == next_frame, f"gap/overlap: {end_frame} vs {next_frame}"


def test_asset_start_uses_embedded_timecode(tmp_path, monkeypatch):
    """A clip stamped 01:00:00:00 must be declared as starting there, or
    DaVinci reports a timecode mismatch and leaves the media offline."""
    clip = tmp_path / "tc.mov"; clip.write_bytes(b"")
    monkeypatch.setattr(_markers, "start_timecode_sec", lambda p, num=1, den=30: 3600.0)
    monkeypatch.setattr(_markers, "get_video_fps", lambda p: 30.0)
    monkeypatch.setattr(_markers, "_video_duration", lambda p, fps: 10.0)
    xml = _markers.fcpxml_for_markers({str(clip): [(1.0, "Sarah")]})
    assert 'start="108000/30s"' in xml           # 3600s at 30fps
    assert 'start="108030/30s"' in xml           # marker 1s later
    assert "<media-rep" in xml                    # modern linking form
    assert 'src=' in xml


def test_missing_timecode_falls_back_to_zero(tmp_path):
    clip = tmp_path / "notc.mov"; clip.write_bytes(b"")
    assert _markers.start_timecode_sec(clip) == 0.0


# --- the user's own data must survive a Spotted run -------------------------
def test_markers_preserve_foreign_markers(tmp_path, have_exiftool=None):
    """A marker a human set in Premiere must survive, with its position."""
    import shutil, subprocess
    if not shutil.which("exiftool"):
        import pytest; pytest.skip("exiftool not on PATH")
    src = tmp_path / "m.mov"
    subprocess.run(["ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10",
                    "-pix_fmt", "yuv420p", "-y", str(src)],
                   capture_output=True, check=False)
    if not src.exists():
        import pytest; pytest.skip("ffmpeg not available")
    subprocess.run(["exiftool", "-overwrite_original", "-q",
                    "-XMP-xmpDM:Markers+={Name=USER MARKER,StartTime=1.750s,Duration=2.0s,Type=Cue}",
                    str(src)], capture_output=True, check=False)
    _markers.write_markers(src, [(0.5, "Sarah")], known_names={"Sarah"})
    back = _markers.read_markers(src)
    assert "USER MARKER" in back, "a human's marker was destroyed"
    assert "1.750s" in back, "the foreign marker lost its position"
    assert "Sarah" in back


def test_marker_names_are_capped_below_exiftool_truncation():
    """exiftool silently writes only the first 1000 list entries and exits 0."""
    assert _markers.MAX_MARKERS_PER_CLIP < 1000


def test_collapse_to_appearances_is_one_marker_per_appearance():
    ev = [(float(t), "Ellie") for t in range(77)]
    assert _markers.collapse_to_appearances(ev) == [(0.0, "Ellie")]
    # leaves and comes back -> two markers
    ev2 = [(float(t), "Ellie") for t in list(range(0, 20)) + list(range(50, 60))]
    assert [t for t, _ in _markers.collapse_to_appearances(ev2)] == [0.0, 50.0]
    # two people on screen together stay separate
    ev3 = [(1.0, "Ellie"), (1.0, "Taylor"), (2.0, "Ellie")]
    assert sorted(_markers.collapse_to_appearances(ev3)) == [(1.0, "Ellie"), (1.0, "Taylor")]


def test_finder_write_preserves_user_tags_and_comment(tmp_path):
    import plistlib, subprocess, shutil
    from facetag import finder as _finder
    if not shutil.which("xattr"):
        import pytest; pytest.skip("xattr unavailable")
    f = tmp_path / "f.mov"; f.write_bytes(b"x")

    def setx(key, val):
        subprocess.run(["xattr", "-wx", key,
                        plistlib.dumps(val, fmt=plistlib.FMT_BINARY).hex(), str(f)], check=True)
    setx(_finder.XATTR_USER_TAGS, ["Important\n6", "Wedding Keep\n0"])
    setx(_finder.XATTR_FINDER_COMMENT, "My note: master take")

    _finder.write_finder_comment(f, ["Sarah", "high energy"])
    tags = _finder._read_xattr(f, _finder.XATTR_USER_TAGS)
    comment = _finder._read_xattr(f, _finder.XATTR_FINDER_COMMENT)
    assert "Important\n6" in tags, "a coloured Finder tag was destroyed"
    assert "Wedding Keep\n0" in tags
    assert "Sarah\n0" in tags
    assert "My note: master take" in comment, "the user's note was destroyed"

    # re-running must not duplicate
    _finder.write_finder_comment(f, ["Sarah", "high energy"])
    tags2 = _finder._read_xattr(f, _finder.XATTR_USER_TAGS)
    assert len(tags2) == len(tags)
    assert (_finder._read_xattr(f, _finder.XATTR_FINDER_COMMENT)).count("Spotted:") == 1


def test_read_keywords_raises_instead_of_reporting_empty(tmp_path):
    """A failed read must not look like 'no keywords', or merge mode silently
    overwrites the user's existing keywords."""
    from facetag import tag as _tag
    import pytest
    with pytest.raises(Exception):
        _tag.read_keywords(tmp_path / "does_not_exist.mov")
