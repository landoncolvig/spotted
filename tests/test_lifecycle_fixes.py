"""Tests for the v0.0.49 correctness + security fixes:
scan-complete flag, marker de-duplication, labeler XSS escaping, and the
faceless-folder cluster exit code."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
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
def _luac(script: str, tmp_path) -> None:
    """Syntax-check with luac when it is installed, else skip that assertion.

    A Lua syntax error is invisible: Resolve lists the menu item and running it
    does nothing at all. There is no other automated way to catch one, since the
    interpreter lives inside Resolve.
    """
    import shutil, subprocess
    luac = shutil.which("luac")
    if not luac:
        return
    f = tmp_path / "s.lua"
    f.write_text(script)
    r = subprocess.run([luac, "-p", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_resolve_marker_script_is_valid_lua_with_data(tmp_path):
    """Lua, not Python. Resolve hides .py from Workspace > Scripts unless it
    finds a python.org framework build, so on an ordinary Mac the script was
    written correctly and never appeared."""
    vm = {
        "/x/A.mov": [(0.5, "Sarah"), (1.0, "Energy peak")],
        "/x/B.mov": [(2.0, "Dad")],
    }
    script = _markers.resolve_marker_script(vm)
    assert script
    _luac(script, tmp_path)
    for token in ("A.mov", "B.mov", "Sarah", "Dad", "Energy peak", "AddMarker", "Yellow", "Blue"):
        assert token in script
    assert _markers.resolve_marker_script({}) == ""   # nothing to mark → empty


def test_resolve_script_survives_hostile_clip_names(tmp_path):
    """Clip names come off the user's disk. One unescaped quote is a syntax
    error, and a broken script is indistinguishable from a missing one."""
    vm = {
        '/x/Ellie\'s "best" clip.mov': [(0.5, 'Say "hi"')],
        "/x/back\\slash.mov": [(1.0, "Dad")],
    }
    script = _markers.resolve_marker_script(vm)
    assert script
    _luac(script, tmp_path)


def test_write_resolve_script_lands_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))         # isolate from the real ~/Library
    vm = {str(tmp_path / "clip.mov"): [(0.5, "Sarah")]}
    out = _markers.write_resolve_script(vm, tmp_path)
    assert out is not None and out.exists() and out.name == "Spotted Markers.lua"
    assert "Sarah" in out.read_text()
    assert _markers.write_resolve_script({}, tmp_path) is None


def test_writing_the_lua_script_retires_the_old_python_one(tmp_path, monkeypatch):
    """Builds before 0.0.71 left a Spotted Markers.py in Resolve's Utility
    folder. Anyone with a framework Python would otherwise get two menu entries
    of the same name, one of them holding markers from an older batch."""
    monkeypatch.setenv("HOME", str(tmp_path))
    util = (tmp_path / "Library/Application Support/Blackmagic Design"
            / "DaVinci Resolve/Fusion/Scripts/Utility")
    util.mkdir(parents=True)
    legacy = util / "Spotted Markers.py"
    legacy.write_text("# stale markers from an older batch")

    out = _markers.write_resolve_script({str(tmp_path / "c.mov"): [(0.5, "Sarah")]}, tmp_path)
    assert out == util / "Spotted Markers.lua"
    assert not legacy.exists(), "the dead Python script must not linger beside the Lua one"


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
    assert _markers.fcpxml_for_markers({}) == ""       # no clips at all

    # A clip with no markers still belongs on the timeline: the caller hands in
    # the whole batch and expects its footage back. Omitting unmarked clips
    # turned a tester's 170-clip batch into a one-clip timeline.
    only_unmarked = _markers.fcpxml_for_markers({str(clip): []})
    assert "asset-clip" in only_unmarked
    assert "<marker" not in only_unmarked
    # The EDL carries markers and nothing else, so with none it stays empty
    # rather than emitting a header Resolve reports as a failed import.
    assert _markers.edl_for_markers({str(clip): []}) == ""


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
    stale = tmp_path / "Spotted Markers.fcpxml"
    stale.write_text("stale export")
    out = _markers.write_fcpxml({str(clip): [(0.5, "Sarah")]}, tmp_path)
    assert out is not None and out.exists()
    assert out.name == "Spotted Markers.fcpxml"
    assert "stale export" not in out.read_text()
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
    stale = tmp_path / "Spotted Markers.edl"
    stale.write_text("stale export")
    out = _markers.write_edl({str(clip): [(0.5, "Sarah")]}, tmp_path)
    assert out is not None and out.exists() and out.name == "Spotted Markers.edl"
    assert "stale export" not in out.read_text()
    assert _markers.write_edl({}, tmp_path) is None


@pytest.mark.parametrize("writer", [_markers.write_fcpxml, _markers.write_edl])
def test_replaced_exports_look_new_in_finder(tmp_path, writer):
    """Overwriting in place reuses the inode and macOS keeps the original
    birthtime, so Finder's Date Created still shows the first export ever
    written to that folder. A tester read that date, saw yesterday, and
    reported a correct run as "the fcpxml didn't update"."""
    import os
    import time

    clip = tmp_path / "a.mov"
    clip.write_bytes(b"")
    first = writer({str(clip): [(0.5, "Ellie")]}, tmp_path)
    assert first is not None
    born_before = os.stat(first).st_birthtime
    time.sleep(1.1)

    second = writer({str(clip): [(0.5, "Rowan")]}, tmp_path)
    assert second == first
    st = os.stat(second)
    assert st.st_birthtime > born_before, "Date Created still shows the old export"
    assert abs(st.st_birthtime - st.st_mtime) < 1.0, "Created and Modified disagree"
    assert "Rowan" in second.read_text()


@pytest.mark.parametrize("writer", [_markers.write_fcpxml, _markers.write_edl])
def test_export_write_failures_are_not_reported_as_empty(
    tmp_path, monkeypatch, writer
):
    clip = tmp_path / "locked.mov"
    clip.write_bytes(b"")
    original_write_text = Path.write_text

    def fail_export(path, *args, **kwargs):
        if path.name in {"Spotted Markers.fcpxml", "Spotted Markers.edl"}:
            raise PermissionError("read-only export folder")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_export)
    with pytest.raises(PermissionError, match="read-only export folder"):
        writer({str(clip): [(0.5, "Sarah")]}, tmp_path)


def test_markers_write_exits_when_the_primary_export_fails(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "index.db"
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"")
    conn = _db.connect(db_path)
    video_id = _db.add_video(conn, str(clip), 4.0)
    _db.set_energy(conn, video_id, 0.9, "high", [1.0])
    _db.set_setting(conn, "last_scan_root", str(tmp_path))
    conn.commit()
    conn.close()

    def fail_fcpxml(_timeline_clips, _out_dir):
        raise PermissionError("read-only export folder")

    monkeypatch.setattr(_markers, "write_fcpxml", fail_fcpxml)
    result = CliRunner().invoke(
        _cli.app,
        ["markers-write", "--db", str(db_path), "--no-sidecar"],
    )

    assert result.exit_code == 4
    assert "Couldn't write DaVinci exports" in result.output
    assert "read-only export folder" in result.output


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


# --- audit fixes: scoping, auth, rescan safety, injection -------------------
def test_rescan_clears_scan_complete_before_wiping_faces(tmp_path):
    """Cancelling a rescan must not leave clips flagged complete with 0 faces."""
    import numpy as np
    db = tmp_path / "r.db"
    conn = _db.connect(db)
    clip = tmp_path / "v.mov"; clip.write_bytes(b"")
    conn.execute("INSERT INTO videos(path) VALUES(?)", (str(clip),))
    vid = conn.execute("SELECT id FROM videos WHERE path=?", (str(clip),)).fetchone()[0]
    conn.execute(
        "INSERT INTO faces(video_id,timestamp_sec,cluster_id,embedding) VALUES(?,?,?,?)",
        (vid, 1.0, 1, np.zeros(512, dtype=np.float32).tobytes()),
    )
    _db.mark_scan_complete(conn, vid)
    conn.commit()
    assert _db.is_scanned(conn, str(clip))

    _db.mark_scan_incomplete(conn, vid)
    _db.clear_video_faces(conn, vid)
    # A cancel landing here must leave the clip re-scannable, not skipped.
    assert not _db.is_scanned(conn, str(clip))


def test_labeler_requires_token_and_loopback_host(tmp_path):
    """No token, wrong token, or a rebound Host must all be refused."""
    from facetag import web as _web
    _db.connect(tmp_path / "t.db").close()
    app = _web.create_app(tmp_path / "t.db", tmp_path / "thumbs")
    _web._install_guard(app, "TOKEN123")
    c = app.test_client()
    assert c.get("/").status_code == 403
    assert c.get("/?k=nope").status_code == 403
    assert c.post("/hide/1").status_code == 403
    assert c.get("/?k=TOKEN123").status_code == 200
    # DNS rebinding: correct token, attacker's Host header
    assert c.get("/?k=TOKEN123", headers={"Host": "evil.com"}).status_code == 403


def test_scope_label_is_escaped(tmp_path):
    """A folder name is user data and lands in markup."""
    from facetag import web as _web
    _db.connect(tmp_path / "s.db").close()
    evil = tmp_path / '<img src=x onerror=alert(1)>'
    evil.mkdir()
    app = _web.create_app(tmp_path / "s.db", tmp_path / "thumbs", scope_paths=[str(evil)])
    _web._install_guard(app, "T")
    body = app.test_client().get("/?k=T").get_data(as_text=True)
    assert "<img src=x onerror=" not in body


def test_scope_helper_matches_only_inside_the_folder():
    from facetag.cli import _under_scope
    root = "/Users/x/Footage/May"
    assert _under_scope("/Users/x/Footage/May/a.mov", root)
    assert _under_scope("/Users/x/Footage/May/sub/b.mov", root)
    # the sibling folder that was silently getting rewritten
    assert not _under_scope("/Users/x/Footage/April/c.mov", root)
    # prefix collision: "MayOld" must not match "May"
    assert not _under_scope("/Users/x/Footage/MayOld/d.mov", root)
    # no scope means the whole library, which is still what Re-tag Library wants
    assert _under_scope("/anything", None)


# --- engine robustness ------------------------------------------------------
def test_unwritable_containers_are_detected_not_attempted():
    """mkv/avi scan fine but exiftool refuses to write them; that must read as
    'skipped', not as a raw exiftool failure."""
    from facetag.tag import can_write_metadata
    from pathlib import Path as P
    for ok in ("a.mov", "b.mp4", "c.M4V"):
        assert can_write_metadata(P(ok))
    for bad in ("d.mkv", "e.avi", "f.webm", "g.WMV", "h.mpg"):
        assert not can_write_metadata(P(bad))


def test_incremental_assign_matches_naive_without_the_huge_allocation():
    """The (n,k,d) broadcast was a 4.1 GB allocation at 5k faces / 200 clusters."""
    import numpy as np
    from facetag import cluster as _cluster
    rng = np.random.default_rng(7)
    new = rng.normal(size=(300, 512)).astype(np.float32)
    cents = {i: rng.normal(size=512) for i in range(25)}
    ids = list(cents.keys())
    C = np.stack([cents[i] for i in ids]).astype(np.float64)
    naive = np.sqrt(((new.astype(np.float64)[:, None, :] - C[None, :, :]) ** 2).sum(-1))
    got = _cluster.incremental_assign(new, cents, 9999, match_threshold=1e9)
    assert list(got) == [ids[i] for i in naive.argmin(1)]


def test_sqlite_is_wal_with_a_real_busy_timeout(tmp_path):
    """The labeler runs concurrently with scans; at the default timeout a name
    typed during a commit raised 'database is locked' and was lost."""
    conn = _db.connect(tmp_path / "w.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_drop_frame_timecode_is_not_treated_as_non_drop(monkeypatch):
    """Ellie's clips carry drop-frame timecode. Counting it as non-drop
    overstates the start by ~0.1%: 4.7s at TC 01:17, 16.3s at TC 04:32. The
    clip then claims to run past the end of its own media and DaVinci shows the
    tail as Media Offline."""
    from pathlib import Path as P

    def probe(_p, args):
        # 59.94, drop-frame (';' before the frames field)
        if "stream=r_frame_rate" in " ".join(args):
            return "60000/1001"
        return "01:17:36;36"

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    num, den = 1001, 60000
    got = _markers.start_timecode_sec(P("x.mov"), num, den)

    naive_frames = ((1 * 60 + 17) * 60 + 36) * 60 + 36
    dropped = 4 * (77 - 7)                     # 4/min except every tenth
    assert round(got * den / num) == naive_frames - dropped
    # and the error we removed is the one she measured
    assert 4.0 < (naive_frames * num / den) - got < 5.5


def test_half_rate_tmcd_frame_field_maps_to_the_video_timeline(tmp_path, monkeypatch):
    """DJI labels this 59.94 video with a 29.97 tmcd track. Its raw ``;14``
    frame field therefore lands on video frame 28. Treating the field as if it
    already counted at 59.94 declared the source 14 video frames early."""
    from pathlib import Path as P

    calls: list[str] = []

    def probe(_p, args):
        query = " ".join(args)
        calls.append(query)
        if "-select_streams d" in query and "stream=avg_frame_rate" in query:
            return "30000/1001"              # tmcd labels half-rate frames
        if "-select_streams d" in query and "stream_tags=timecode" in query:
            return "19:22:57;14"
        if "-select_streams v:0" in query and "stream=r_frame_rate" in query:
            return "60000/1001"              # source video and XML timeline
        if "-select_streams v:0" in query and "stream_tags=timecode" in query:
            return "19:22:57;14"
        if "format_tags=timecode" in query:
            return "19:22:57;14"
        return ""

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    num, den = 1001, 60000
    got = _markers.start_timecode_sec(P("dji.mp4"), num, den)

    expected_frames = 4_182_464             # 19:22:57;14 @ 29.97 -> ;28 @ 59.94
    assert round(got * den / num) == expected_frames
    assert _markers._fcp_time(got, num, den) == "4186646464/60000s"
    assert not any("format_tags=timecode" in query for query in calls)

    clip = tmp_path / "dji.mp4"
    clip.write_bytes(b"")
    monkeypatch.setattr(_markers, "get_video_fps", lambda _p: 60000 / 1001)
    monkeypatch.setattr(_markers, "_video_duration", lambda _p, _fps: 1.0)
    xml = _markers.fcpxml_for_markers({str(clip): [(0.0, "Ellie")]})
    assert '<asset id="a1" name="dji" start="4186646464/60000s"' in xml
    assert '<asset-clip ref="a1" name="dji" offset="0/60000s" start="4186646464/60000s"' in xml


@pytest.mark.parametrize("frames_field", [0, 2, 4, 8, 12, 14, 15, 29])
def test_every_clip_lands_on_its_own_frame_regardless_of_the_frames_field(
    monkeypatch, frames_field
):
    """Why the tester saw "off 8, 2, 15, 12, 4, and other random numbers".

    Reading a 29.97 tmcd field at the 59.94 video rate cancels out in the
    hours/minutes/seconds and in the drop-frame correction, so the whole error
    is the timecode's frames field: each clip was off by its own ``;ff``, a
    different number in 0..29 every time. One clip lining up proves nothing on
    its own, so pin the whole range.
    """
    from pathlib import Path as P

    def probe(_p, args):
        query = " ".join(args)
        if "-select_streams d" in query and "stream=avg_frame_rate" in query:
            return "30000/1001"
        if "-select_streams d" in query and "stream_tags=timecode" in query:
            return f"01:17:36;{frames_field:02d}"
        return "60000/1001"

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    got = _markers.start_timecode_sec(P("dji.mp4"), 1001, 60000)

    total_min = 1 * 60 + 17
    dropped = round(30 * 0.066666) * (total_min - total_min // 10)
    correct = (
        (((1 * 60 + 17) * 60 + 36) * 30 + frames_field - dropped) / (30000 / 1001)
    )
    off_by = (got - correct) * 60000 / 1001
    assert abs(off_by) < 1e-6, f"{frames_field=} lands {off_by:+.2f} frames out"


def test_a_clip_slot_never_cuts_the_last_frame(tmp_path, monkeypatch):
    """A batch can mix frame rates. 384 frames of 30.00fps footage is 767.23
    frames of a 59.94 timeline; rounding to nearest handed that clip a
    767-frame slot and cut its tail. The tester saw "a clip every once in a
    while that's missing the last frame" — only the ones whose length landed
    in the lower half of a frame, which is why it looked intermittent."""
    spf = 1001 / 60000
    cases = {                       # source frames @30.00fps -> exact 59.94 frames
        "a.mov": (384, 767.233),    # rounds DOWN: the reported bug
        "b.mov": (300, 599.401),    # rounds DOWN
        "c.mov": (250, 499.500),    # rounds up already
        "d.mov": (600, 1198.801),   # rounds up already
    }
    durations = {}
    vm = {}
    for name, (src_frames, _) in cases.items():
        f = tmp_path / name
        f.write_bytes(b"")
        durations[str(f)] = src_frames / 30.0
        vm[str(f)] = [(0.5, "Ellie")]

    monkeypatch.setattr(_markers, "get_video_fps", lambda _p: 60000 / 1001)
    monkeypatch.setattr(
        _markers, "_video_duration", lambda p, _fps: durations[str(p)]
    )
    _num, _den, entries = _markers._timeline_layout(vm)

    for p, _off, dur, _e in entries:
        real = durations[str(p)]
        assert dur >= real - 1e-9, (
            f"{p.name}: {(real - dur)/spf:.3f} frames of media left off the slot"
        )
        # And never more than a whole frame of padding past the media.
        assert dur - real < spf, f"{p.name}: slot overruns the media by a frame or more"


def test_unwritable_containers_are_skipped_not_failed(tmp_path):
    """exiftool can only write into mp4/mov/m4v. A .mkv or .mts clip still
    belongs on the timeline with its markers in the EDL, which is how DaVinci
    reads them anyway, so a container that cannot hold in-file markers is a
    skip. markers-write had no writability check at all, so those clips came
    back as per-clip exiftool failures on footage that was otherwise fine."""
    db_path = tmp_path / "index.db"
    conn = _db.connect(db_path)
    for name in ("a.mkv", "b.mts", "c.mp4"):
        f = tmp_path / name
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 2.0)
        _db.mark_scan_complete(conn, vid)
        _db.set_energy(conn, vid, 0.9, "high", [0.5])
    _db.set_setting(conn, "last_scan_roots", json.dumps([str(tmp_path)]))
    conn.commit()
    conn.close()

    res = CliRunner().invoke(
        _cli.app, ["markers-write", "--db", str(db_path), "--no-sidecar"]
    )
    assert res.exit_code == 0, res.output
    events = [
        json.loads(l[len("__SPOTTED__ "):])
        for l in res.output.splitlines() if l.startswith("__SPOTTED__ ")
    ]
    by = {e["event"]: e for e in events}

    assert by["markers-unwritable"]["count"] == 2
    assert {e["name"] for e in events if e["event"] == "markers-skip"} == {"a.mkv", "b.mts"}
    # The contract is that an unwritable CONTAINER is never reported as a
    # failure. (c.mp4 is a zero-byte fixture, so exiftool fails on it for a
    # real reason; that is the fixture, not the behaviour under test.)
    errored = {e["name"] for e in events if e["event"] == "markers-error"}
    assert not (errored & {"a.mkv", "b.mts"}), f"unwritable format reported as failure: {errored}"
    # And every clip still reaches the timeline regardless of container.
    assert by["markers-summary"]["timeline_clips"] == 3
    assert (tmp_path / "Spotted Markers.fcpxml").exists()
    assert (tmp_path / "Spotted Markers.edl").exists()


def test_writability_is_an_allowlist_so_new_formats_default_to_safe(tmp_path):
    """A denylist meant every newly-supported container defaulted to "try it",
    so adding AVCHD for a Sony shooter would have turned "no videos found"
    into a wall of exiftool errors."""
    from facetag import extract as _extract
    from facetag import tag as _tag

    assert _tag.can_write_metadata(Path("x.mp4")) is True
    assert _tag.can_write_metadata(Path("x.mov")) is True
    for ext in (".mkv", ".mts", ".m2ts", ".mxf", ".avi", ".webm", ".sonyfuture"):
        assert _tag.can_write_metadata(Path(f"x{ext}")) is False, ext
    # Sony/Panasonic camcorder footage must at least be seen and scanned.
    for ext in (".mts", ".m2ts", ".mxf"):
        f = tmp_path / f"clip{ext}"
        f.write_bytes(b"")
        assert _extract.is_video(f) is True, ext


def test_timeline_takes_the_majority_frame_rate_and_size(tmp_path, monkeypatch):
    """One phone clip in a folder of drone footage must not decide the rate or
    the frame size for the other hundred. A clip whose rate is not a clean
    multiple of the timeline's has to be padded to keep its last frame, so the
    majority rate leaves that padding on the fewest clips. Frame size matters
    the same way: a portrait clip should not turn the timeline sideways."""
    shapes = {}
    vm = {}
    for i in range(5):
        f = tmp_path / f"c{i}.mov"
        f.write_bytes(b"")
        # four landscape 59.94 clips, one portrait 30fps phone clip
        shapes[str(f)] = (60000 / 1001, (1920, 1080)) if i else (30.0, (1080, 1920))
        vm[str(f)] = [(0.5, "Ellie")]

    monkeypatch.setattr(_markers, "get_video_fps", lambda p: shapes[str(p)][0])
    monkeypatch.setattr(_markers, "get_video_size", lambda p: shapes[str(p)][1])
    monkeypatch.setattr(_markers, "_video_duration", lambda _p, _f: 2.0)

    num, den, _entries = _markers._timeline_layout(vm)
    assert (num, den) == (1001, 60000), "minority phone clip captured the rate"

    xml = _markers.fcpxml_for_markers(vm)
    assert 'width="1920" height="1080"' in xml, "minority clip turned the timeline"

    # Flip the majority and both must follow it.
    for k in list(shapes):
        shapes[k] = (30.0, (1080, 1920))
    num, den, _e = _markers._timeline_layout(vm)
    assert (num, den) == (1, 30)
    assert 'width="1080" height="1920"' in _markers.fcpxml_for_markers(vm)


def test_duration_measures_the_picture_not_the_container(monkeypatch):
    """Camera audio does not end on a video frame boundary: AAC packets are
    1024 samples (21.3ms at 48kHz) against 16.7ms for a 59.94 frame, so the
    container outlives the last frame by a fraction. Reading format=duration
    and rounding up turned every one of those fractions into a whole frame of
    nothing, which the tester saw as "most of the clips have an empty frame"."""
    from pathlib import Path as P

    def probe(_p, args):
        query = " ".join(args)
        if "stream=nb_frames" in query:
            return "768"
        if "avg_frame_rate" in query:
            return "60000/1001"
        if "stream=duration" in query:
            return "12.812800"
        if "format=duration" in query:
            return "12.818005"      # audio tail, 5ms past the last frame
        return ""

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    dur = _markers._video_duration(P("dji.MP4"), 60000 / 1001)
    spf = 1001 / 60000
    assert abs(dur / spf - 768) < 1e-6, f"{dur/spf} frames, expected exactly 768"
    # and the slot it produces holds no empty frame
    import math
    assert math.ceil(dur / spf - _markers._FRAME_EPSILON) == 768


def test_duration_falls_back_when_the_frame_count_is_unavailable(monkeypatch):
    """Some containers report nb_frames as N/A. The stream's own duration is
    still better than the container's, which includes the audio tail."""
    from pathlib import Path as P

    def probe(_p, args):
        query = " ".join(args)
        if "stream=nb_frames" in query:
            return "N/A"
        if "avg_frame_rate" in query:
            return "0/0"
        if "r_frame_rate" in query:
            return "60000/1001"
        if "stream=duration" in query:
            return "12.812800"
        if "format=duration" in query:
            return "12.818005"
        return ""

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    assert _markers._video_duration(P("x.MP4"), 60000 / 1001) == 12.8128


def test_evenly_divisible_clips_are_not_given_a_phantom_frame(tmp_path, monkeypatch):
    """Rounding up must not hand a frame to a clip that already fits. ffprobe
    reports duration to six decimals, so an exact clip can measure a hair long."""
    clip = tmp_path / "exact.mov"
    clip.write_bytes(b"")
    spf = 1001 / 60000
    monkeypatch.setattr(_markers, "get_video_fps", lambda _p: 60000 / 1001)
    for measured in (767 * spf, 767 * spf + 1e-7, 767 * spf - 1e-7):
        monkeypatch.setattr(_markers, "_video_duration", lambda _p, _f, m=measured: m)
        _n, _d, entries = _markers._timeline_layout({str(clip): [(0.1, "E")]})
        assert round(entries[0][2] / spf) == 767, f"{measured!r} drifted off 767"


def test_non_drop_timecode_is_unchanged(monkeypatch):
    from pathlib import Path as P

    def probe(_p, args):
        if "stream=r_frame_rate" in " ".join(args):
            return "30/1"
        return "01:00:00:00"                   # ':' -> non-drop

    monkeypatch.setattr(_markers, "_ffprobe_field", probe)
    got = _markers.start_timecode_sec(P("x.mov"), 1, 30)
    assert abs(got - 3600.0) < 1e-6


def test_timecoded_mp4s_keep_full_slots_in_the_timeline(tmp_path, monkeypatch):
    """Shortening every edit by two frames moved each later clip two more
    frames left: 0, -2, -4, -6, -8 in Ellie's five-clip batch. Preserve the
    full source duration until a source-remapping fix is proven in Resolve."""
    clips = [tmp_path / f"dji-{i}.mp4" for i in range(3)]
    for clip in clips:
        clip.write_bytes(b"")
    monkeypatch.setattr(_markers, "get_video_fps", lambda _p: 60000 / 1001)
    monkeypatch.setattr(_markers, "_video_duration", lambda _p, _fps: 10.01)
    vm = {str(clip): [] for clip in clips}
    num, den, entries = _markers._timeline_layout(vm)
    spf = num / den
    assert [round(offset / spf) for _p, offset, _dur, _events in entries] == [
        0, 600, 1200
    ]
    assert [round(dur / spf) for _p, _offset, dur, _events in entries] == [
        600, 600, 600
    ]


def test_each_clip_uses_its_own_embedded_start_timecode(tmp_path, monkeypatch):
    clips = [tmp_path / "camera-a.mp4", tmp_path / "camera-b.mp4"]
    for clip in clips:
        clip.write_bytes(b"")
    starts = {clips[0]: 3600.0, clips[1]: 7200.0}
    monkeypatch.setattr(_markers, "get_video_fps", lambda _p: 30.0)
    monkeypatch.setattr(_markers, "_video_duration", lambda _p, _fps: 5.0)
    monkeypatch.setattr(
        _markers,
        "start_timecode_sec",
        lambda p, num=1, den=30: starts[p],
    )

    xml = _markers.fcpxml_for_markers({str(clip): [] for clip in clips})
    assert 'start="108000/30s"' in xml
    assert 'start="216000/30s"' in xml


def test_scope_falls_back_to_the_recorded_batch(tmp_path):
    """The UI's folder path is a module global that dies with the app, and the
    auto-update restarts the app. Without a DB-side fallback, finishing a batch
    after an update silently widened every write to the whole library."""
    from facetag.cli import _resolve_scope
    conn = _db.connect(tmp_path / "s.db")
    assert _resolve_scope(None, conn) is None          # nothing recorded yet
    # A library last scanned by a build that predates multi-path batches only
    # has the single-root setting, and must still scope to it rather than
    # silently widening to everything.
    _db.set_setting(conn, "last_scan_root", "/Users/x/May")
    assert _resolve_scope(None, conn) == ["/Users/x/May"]
    # A batch is every path the user handed over, not just the first. Scoping
    # to paths[0] is what turned a 170-file drop into a one-clip timeline.
    _db.set_setting(conn, "last_scan_roots", json.dumps(["/Users/x/a.mov", "/Users/x/b.mov"]))
    assert _resolve_scope(None, conn) == ["/Users/x/a.mov", "/Users/x/b.mov"]
    # explicit scope still wins, and --all means the whole library on purpose
    assert _resolve_scope(tmp_path, conn) == [str(tmp_path.resolve())]
    assert _resolve_scope(None, conn, allow_all=True) is None


def test_tag_vocabulary_is_scoped_to_the_batch(tmp_path):
    """Unioned library-wide, a Vegas trip gets searched for 'conference room'
    and 'sticky notes' typed months earlier for different footage."""
    conn = _db.connect(tmp_path / "v.db")
    new_dir = tmp_path / "vegas"
    old_dir = tmp_path / "office"
    for d, tag in ((new_dir, "casino"), (old_dir, "conference room")):
        d.mkdir()
        clip = d / "c.mov"
        clip.write_bytes(b"")
        conn.execute("INSERT INTO videos(path) VALUES(?)", (str(clip),))
        vid = conn.execute("SELECT id FROM videos WHERE path=?", (str(clip),)).fetchone()[0]
        _db.set_batch_tags(conn, vid, [tag])
    conn.commit()
    assert _db.all_batch_tags(conn) == ["casino", "conference room"]
    assert _db.all_batch_tags(conn, str(new_dir)) == ["casino"]


# --- DaVinci export location + stale cleanup -------------------------------
# A tester imported a "Spotted Markers.fcpxml" she found several folders above
# her footage and got a timeline full of clips she had never scanned. The
# export had been written to the clips' common ancestor by an earlier,
# wider run and was never cleaned up.

def test_export_lands_in_the_scanned_folder_not_the_common_ancestor(tmp_path):
    from facetag import cli

    vegas = tmp_path / "Youtube" / "LasVegas"
    older = tmp_path / "Youtube" / "Older"
    vegas.mkdir(parents=True)
    older.mkdir(parents=True)
    markers = {str(vegas / "a.mp4"): [(1.0, "E")], str(older / "b.mp4"): [(1.0, "E")]}

    # Without a scope we still fall back to the shared ancestor...
    assert cli._export_dir(markers, None).name == "Youtube"
    # ...but a scoped batch writes into the folder the user actually dropped.
    assert cli._export_dir(markers, str(vegas)) == vegas


def test_export_dir_never_resolves_to_a_filesystem_root():
    from facetag import cli

    # An external drive plus the internal one share only "/" — and on macOS
    # commonpath returns that rather than raising.
    assert cli._export_dir({"/Volumes/Ext/a.mp4": [], "/Users/x/b.mp4": []}, None) != Path("/")
    assert cli._export_dir({"/Volumes/A/a.mp4": [], "/Volumes/B/b.mp4": []}, None) != Path("/Volumes")


def test_stale_exports_are_removed_but_only_our_own_files(tmp_path):
    from facetag import cli
    from facetag import db as _db

    conn = _db.connect(tmp_path / "i.db")
    old_dir = tmp_path / "Youtube"
    new_dir = tmp_path / "Youtube" / "LasVegas"
    new_dir.mkdir(parents=True)

    stale = old_dir / "Spotted Markers.fcpxml"
    stale.write_text("from an older, wider run")
    theirs = old_dir / "Spotted Markers.txt"       # not a name we ever write
    theirs.write_text("a file the user made")
    cli._record_exports(conn, [stale, theirs])

    removed = cli._clear_stale_exports(conn, new_dir)

    assert [Path(r).name for r in removed] == ["Spotted Markers.fcpxml"]
    assert not stale.exists()
    assert theirs.exists(), "only Spotted's own export names may be deleted"


def test_current_export_is_not_deleted_before_being_rewritten(tmp_path):
    from facetag import cli
    from facetag import db as _db

    conn = _db.connect(tmp_path / "i.db")
    d = tmp_path / "LasVegas"
    d.mkdir()
    cur = d / "Spotted Markers.fcpxml"
    cur.write_text("this run's output")
    cli._record_exports(conn, [cur])

    assert cli._clear_stale_exports(conn, d) == []
    assert cur.exists()


# --- index self-healing ----------------------------------------------------
# A tester reorganised her footage after scanning it. The index kept pointing
# at the old layout, so 169 of her 170 clips were counted as real right up to
# the timeline, where the file-exists check finally dropped them.

def test_forget_missing_videos_drops_only_absent_clips_under_the_root(tmp_path):
    from facetag import db as _db

    conn = _db.connect(tmp_path / "i.db")
    root = tmp_path / "Trip"
    (root / "CamA").mkdir(parents=True)
    here = root / "CamA" / "present.mp4"
    here.write_bytes(b"")
    _db.add_video(conn, str(here), 1.0)
    _db.add_video(conn, str(root / "CamA" / "moved.mp4"), 1.0)   # never existed
    outside = tmp_path / "Elsewhere" / "other.mp4"                # absent, out of scope
    _db.add_video(conn, str(outside), 1.0)

    gone = _db.forget_missing_videos(conn, str(root))

    assert gone == [str(root / "CamA" / "moved.mp4")]
    remaining = {p for (p,) in conn.execute("SELECT path FROM videos").fetchall()}
    assert str(here) in remaining, "a clip that is present must survive"
    assert str(outside) in remaining, "clips outside the scanned root are not touched"


def test_forgetting_a_video_takes_its_faces_with_it(tmp_path):
    from facetag import db as _db

    conn = _db.connect(tmp_path / "i.db")
    root = tmp_path / "Trip"
    root.mkdir()
    vid = _db.add_video(conn, str(root / "ghost.mp4"), 1.0)
    conn.execute(
        "INSERT INTO faces(video_id, timestamp_sec, embedding) VALUES (?, ?, ?)",
        (vid, 1.0, b"\x00" * 8),
    )
    conn.commit()

    _db.forget_missing_videos(conn, str(root))

    left = conn.execute("SELECT COUNT(*) FROM faces WHERE video_id = ?", (vid,)).fetchone()[0]
    assert left == 0, "orphaned face rows would keep the ghost alive in every query"


def test_a_multi_file_drop_scopes_to_every_file_not_just_the_first(tmp_path):
    """A tester selected 170 clips in Finder, dragged them onto Spotted, and got
    back a timeline holding 1. macOS delivers every selected path on a
    multi-selection drag; Spotted took paths[0] and recorded it as the batch, so
    the scope check discarded the other 169 at the timeline step."""
    from facetag.cli import _under_scope
    picked = [f"/Users/e/Video/DJI_{i:04d}.MP4" for i in range(3)]
    assert _under_scope(picked[0], picked)
    assert _under_scope(picked[2], picked), "later files in the selection are in the batch"
    assert not _under_scope("/Users/e/Video/NOT_PICKED.MP4", picked)
    # Scoping to only the first path is precisely the old bug.
    assert not _under_scope(picked[2], [picked[0]])


def test_a_folder_root_still_covers_everything_inside_it(tmp_path):
    """Multi-path scoping must not regress the ordinary case: drop one folder,
    get every clip under it, including in subfolders."""
    from facetag.cli import _under_scope
    roots = ["/Users/e/LasVegas"]
    assert _under_scope("/Users/e/LasVegas/OP3/Video/a.MP4", roots)
    assert _under_scope("/Users/e/LasVegas", roots)
    # A sibling folder sharing a name prefix is a different folder.
    assert not _under_scope("/Users/e/LasVegas2/a.MP4", roots)


def test_scan_walks_every_dropped_path_and_deduplicates(tmp_path):
    """Two roots can overlap (a folder plus a clip inside it). Every clip must
    be scanned once: twice is wasted minutes on a long batch."""
    from facetag import extract as _extract
    folder = tmp_path / "Video"
    folder.mkdir()
    clips = []
    for name in ("a.mov", "b.mov", "c.mov"):
        f = folder / name
        f.write_bytes(b"")
        clips.append(f)

    seen: set[str] = set()
    videos: list[Path] = []
    for p in [folder, clips[0]]:            # overlapping roots, as scan() gets them
        for v in _extract.walk_videos(p):
            key = str(v.resolve())
            if key not in seen:
                seen.add(key)
                videos.append(v)
    assert [v.name for v in videos] == ["a.mov", "b.mov", "c.mov"]


def test_export_lands_beside_the_clips_for_a_multi_file_batch(tmp_path):
    """With no dropped folder to aim at, the export still has to land where the
    footage is rather than at some ancestor the user never opens."""
    from facetag.cli import _export_dir
    folder = tmp_path / "OP3" / "Video"
    folder.mkdir(parents=True)
    picked = []
    for name in ("a.mov", "b.mov"):
        f = folder / name
        f.write_bytes(b"")
        picked.append(str(f))
    assert _export_dir({p: [] for p in picked}, picked) == folder


def test_tag_write_scope_line_does_not_crash(tmp_path):
    """v0.0.70 shipped a NameError here. The status line still said
    `Path(root).name` after the scope became a list and the local `root` was
    removed, and it only runs when scoping actually drops a clip, so every test
    and every local run walked straight past it. The tester's batch hit it and
    the whole run died with "Couldn't finish"."""
    db_path = tmp_path / "t.db"
    conn = _db.connect(db_path)
    inside, outside = tmp_path / "a.mov", tmp_path / "b.mov"
    for f in (inside, outside):
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 3.0)
        _db.set_energy(conn, vid, 0.9, "high", [1.0])
    # Scope to one of the two, so `before != len(mapping)` and the line runs.
    _db.set_setting(conn, "last_scan_roots", json.dumps([str(inside)]))
    conn.commit()
    conn.close()

    res = CliRunner().invoke(_cli.app, ["tag-write", "--db", str(db_path), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Scoped to" in res.output
    assert "1 of 2 clip(s)" in res.output
    assert "NameError" not in res.output


def test_scope_label_names_a_batch_of_any_shape(tmp_path):
    """One folder, a Finder multi-selection out of one folder, and a pile
    spanning folders all have to render as something a human recognises."""
    from facetag.cli import _scope_label
    assert _scope_label(["/Users/e/LasVegas"]) == "LasVegas"
    assert _scope_label(["/Users/e/Video/a.MP4", "/Users/e/Video/b.MP4"]) == "2 clip(s) in Video"
    assert _scope_label(["/Users/e/OP3/a.MP4", "/Users/e/OP4/b.MP4"]) == "2 item(s)"
    assert _scope_label(None) == "everything"


def test_batch_tag_vocabulary_accepts_a_list_of_roots(tmp_path):
    """Second casualty of the same v0.0.70 refactor: all_batch_tags still did
    `scope_root.rstrip("/")` on what became a list, so a multi-file batch blew
    up with AttributeError at the activity-suggest step."""
    conn = _db.connect(tmp_path / "v.db")
    picked, other = [], None
    for name, tag in (("a.mov", "vegas"), ("b.mov", "pool"), ("c.mov", "conference room")):
        f = tmp_path / name
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 1.0)
        _db.set_batch_tags(conn, vid, [tag])
        if name != "c.mov":
            picked.append(str(f))
        else:
            other = str(f)
    conn.commit()

    assert _db.all_batch_tags(conn, picked) == ["pool", "vegas"]
    # The clip the user did not hand over keeps its vocabulary to itself.
    assert "conference room" not in _db.all_batch_tags(conn, picked)
    # A single string still works, for libraries scanned before 0.0.70.
    assert _db.all_batch_tags(conn, other) == ["conference room"]


def test_activity_suggest_survives_a_multi_file_batch(tmp_path):
    """End of the same chain: the CLI step that reads the vocabulary must not
    crash when the recorded batch is a list of files."""
    db_path = tmp_path / "a.db"
    conn = _db.connect(db_path)
    roots = []
    for name in ("a.mov", "b.mov"):
        f = tmp_path / name
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 1.0)
        _db.set_batch_tags(conn, vid, ["vegas"])
        roots.append(str(f))
    _db.set_setting(conn, "last_scan_roots", json.dumps(roots))
    conn.commit()
    conn.close()

    res = CliRunner().invoke(_cli.app, ["activity-suggest", "--db", str(db_path)])
    assert res.exception is None or isinstance(res.exception, SystemExit), repr(res.exception)
    assert res.exit_code == 0, res.output


def test_timeline_is_written_when_no_clip_carries_a_marker(tmp_path):
    """A tester dropped one aerial clip with no faces in it and no energy peak.

    markers-write bailed before the export block, so no FCPXML and no EDL were
    written at all and the Done screen showed four empty rows with no error to
    explain them. She read that as the app silently doing nothing, which is
    exactly what it did. The batch she handed over still has to come back as a
    timeline; markers are what ride on top of it, not the reason to build it.
    """
    db_path = tmp_path / "index.db"
    clip = tmp_path / "DJI_0029.mov"
    clip.write_bytes(b"")
    conn = _db.connect(db_path)
    vid = _db.add_video(conn, str(clip), 4.0)
    _db.mark_scan_complete(conn, vid)
    # No named face, and an energy bucket without a single peak — the shape of
    # a clip that scores medium overall but never spikes.
    _db.set_energy(conn, vid, 0.4, "medium", [])
    _db.set_setting(conn, "last_scan_roots", json.dumps([str(clip)]))
    conn.commit()
    conn.close()

    res = CliRunner().invoke(
        _cli.app, ["markers-write", "--db", str(db_path), "--no-sidecar"]
    )
    assert res.exception is None or isinstance(res.exception, SystemExit), repr(res.exception)
    assert res.exit_code == 0, res.output

    assert (tmp_path / "Spotted Markers.fcpxml").exists(), (
        "the batch came back without a timeline:\n" + res.output
    )
    events = [
        json.loads(line[len("__SPOTTED__ "):])
        for line in res.output.splitlines()
        if line.startswith("__SPOTTED__ ")
    ]
    kinds = {e["event"] for e in events}
    assert "resolve-timeline" in kinds, f"no timeline event, got {kinds}"
    # The Done screen's coverage row reads as "empty" without this, which is
    # what made a working run look like a failed one.
    summary = next(e for e in events if e["event"] == "markers-summary")
    assert summary["timeline_clips"] == 1
    assert summary["marked_clips"] == 0


def test_status_counts_the_batch_not_the_library(tmp_path):
    """The finish line told a tester "tagged 95 people across 169 clips" after
    she dropped a single clip. It was reading library totals. --batch answers
    the question the Done screen is actually asking, and must come back under
    a different event name so the Library view never renders one drop as the
    user's whole library."""
    db_path = tmp_path / "index.db"
    conn = _db.connect(db_path)
    dropped, other = None, None
    for i, name in enumerate(("dropped.mov", "old_a.mov", "old_b.mov")):
        f = tmp_path / name
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 4.0)
        _db.mark_scan_complete(conn, vid)
        # Everyone in the library has a face; only Rowan is in the batch.
        cluster = 0 if name == "dropped.mov" else i
        conn.execute(
            "INSERT INTO faces(video_id, timestamp_sec, embedding, cluster_id) "
            "VALUES(?, 0.0, X'00', ?)",
            (vid, cluster),
        )
        conn.execute(
            "INSERT OR IGNORE INTO people(cluster_id, name) VALUES(?, ?)",
            (cluster, {0: "Rowan", 1: "Jorja", 2: "Maggie"}[cluster]),
        )
        if name == "dropped.mov":
            dropped = str(f)
        else:
            other = str(f)
    _db.set_setting(conn, "last_scan_roots", json.dumps([dropped]))
    conn.commit()
    conn.close()

    def events(args):
        res = CliRunner().invoke(_cli.app, args)
        assert res.exit_code == 0, res.output
        return [
            json.loads(l[len("__SPOTTED__ "):])
            for l in res.output.splitlines() if l.startswith("__SPOTTED__ ")
        ]

    batch = next(
        e for e in events(["status", "--db", str(db_path), "--batch"])
        if e["event"] in {"batch-stats", "library-stats"}
    )
    assert batch["event"] == "batch-stats", "a scoped count must not pose as library-stats"
    assert batch["videos"] == 1, f"counted the library, not the drop: {batch}"
    assert [p["name"] for p in batch["people"]] == ["Rowan"]
    assert batch["named"] == 1

    # Unscoped is still the whole index, which is what the Library view reads.
    lib = next(
        e for e in events(["status", "--db", str(db_path)])
        if e["event"] in {"batch-stats", "library-stats"}
    )
    assert lib["event"] == "library-stats"
    assert lib["videos"] == 3 and lib["named"] == 3
    assert other is not None


def test_batch_stats_admits_when_it_does_not_know_the_batch(tmp_path):
    """Without a recorded batch, --batch can only report library totals. It
    must label them as such: silently relabelling the library as the batch is
    the bug --batch exists to prevent, and it would revert invisibly."""
    db_path = tmp_path / "index.db"
    conn = _db.connect(db_path)
    for name in ("a.mov", "b.mov"):
        f = tmp_path / name
        f.write_bytes(b"")
        _db.mark_scan_complete(conn, _db.add_video(conn, str(f), 4.0))
    # No last_scan_roots / last_scan_root at all.
    conn.commit()
    conn.close()

    res = CliRunner().invoke(_cli.app, ["status", "--db", str(db_path), "--batch"])
    assert res.exit_code == 0, res.output
    stats = next(
        json.loads(l[len("__SPOTTED__ "):])
        for l in res.output.splitlines()
        if l.startswith("__SPOTTED__ ") and "stats" in l
    )
    assert stats["event"] == "batch-stats"
    assert stats["known"] is False, "library totals posing as the batch"
    assert stats["videos"] == 2


def test_every_pipeline_step_survives_a_multi_file_batch(tmp_path):
    """The gap that let two crashes reach a tester in one day.

    Each step was unit-tested on its own, but nothing ran them in the order the
    app runs them, over a batch shaped the way a Finder multi-selection shapes
    one. Both v0.0.70 regressions (NameError in tag-write, AttributeError in
    activity-suggest) sat on that path and shipped twice.

    The scope must EXCLUDE at least one indexed clip: the tag-write status line
    that raised NameError only executes when scoping actually drops something,
    so a batch matching its scope exactly walks straight past it.
    """
    db_path = tmp_path / "pipe.db"
    conn = _db.connect(db_path)
    handed_over, left_out = [], None
    for i, name in enumerate(("a.mov", "b.mov", "c.mov")):
        f = tmp_path / name
        f.write_bytes(b"")
        vid = _db.add_video(conn, str(f), 4.0)
        _db.set_batch_tags(conn, vid, ["vegas"])
        _db.set_energy(conn, vid, 0.9, "high", [1.0, 2.5])
        if name == "c.mov":
            left_out = str(f)
        else:
            handed_over.append(str(f))
    _db.set_setting(conn, "last_scan_roots", json.dumps(handed_over))
    _db.set_setting(conn, "last_scan_root", handed_over[0])
    conn.commit()
    conn.close()

    runner = CliRunner()
    for args in (
        ["activity-suggest", "--db", str(db_path)],
        ["tag-write", "--db", str(db_path), "--dry-run"],
        ["markers-write", "--db", str(db_path), "--no-resolve", "--no-sidecar"],
    ):
        res = runner.invoke(_cli.app, args)
        # CliRunner swallows the exception into res.exception and prints
        # nothing, so asserting on res.output here would pass for a crash.
        assert res.exception is None or isinstance(res.exception, SystemExit), (
            f"{args[0]} raised {res.exception!r}"
        )
        assert res.exit_code == 0, f"{args[0]} exited {res.exit_code}:\n{res.output}"

    # And the scoping those steps rely on is real, not vacuous.
    conn = _db.connect(db_path)
    roots = _cli._resolve_scope(None, conn)
    assert len(roots) == 2
    assert not _cli._under_scope(left_out, roots), "the third clip was never handed over"
