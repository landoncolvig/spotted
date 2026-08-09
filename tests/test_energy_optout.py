"""Turning energy off has to reach clips that were already scored.

Energy was the one thing Spotted wrote into people's files with no review step
and no way to decline it, unlike faces and matched tags which both have one.

The trap this covers: `scan --no-energy` alone looks like a complete opt-out
and is not. `videos.energy_bucket` and the peak rows persist in the index, so a
clip scored on any earlier drop keeps them forever. An opt-out wired only to
the scan would appear to work on a fresh folder and silently do nothing on a
re-drop, which is the case a real library is almost always in.

So the switch acts at all three stages, and these tests exercise the two that
operate on already-scored data.
"""

from __future__ import annotations

import re
from pathlib import Path

from facetag import db as _db
from facetag import tag as _tag

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
LIB_RS = (ROOT / "app/src-tauri/src/lib.rs").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()


def _scored_library(tmp_path: Path):
    """A clip already carrying an energy bucket, as a re-drop would find it."""
    conn = _db.connect(tmp_path / "t.db")
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"")
    vid = _db.add_video(conn, str(clip), 2.0)
    conn.execute(
        "UPDATE videos SET energy_bucket='high' WHERE id=?", (vid,)
    )
    # A named face, so the clip still has a reason to be in the mapping.
    conn.execute("INSERT INTO people(cluster_id,name) VALUES(3,'Ellie')")
    conn.execute(
        "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
        "VALUES(?,?,X'00',3)", (vid, 0.0),
    )
    conn.commit()
    return conn, str(clip)


def test_an_already_scored_clip_still_gets_its_energy_keyword_by_default(tmp_path):
    conn, clip = _scored_library(tmp_path)
    kw = _tag.videos_with_keywords(conn)
    assert "high energy" in kw[clip]


def test_opting_out_drops_it_from_a_clip_scored_on_an_earlier_drop(tmp_path):
    """The whole point. scan --no-energy cannot reach this clip."""
    conn, clip = _scored_library(tmp_path)
    kw = _tag.videos_with_keywords(conn, include_energy=False)
    assert "high energy" not in kw[clip]
    assert "Ellie" in kw[clip], "opting out of energy must not drop the names"


def test_opting_out_is_not_routed_through_the_exclude_set(tmp_path):
    """`exclude_tags` is persisted as review rejections and deletes matching
    rows from auto_tags. Suppressing energy that way would delete a user's own
    tag if they had typed "high energy" as one."""
    assert "delete_auto_tags_by_name(conn, exclude)" in CLI_PY
    sig = CLI_PY[CLI_PY.index("def tag_write"):]
    sig = sig[:sig.index('"""')]
    assert "--energy-keywords/--no-energy-keywords" in sig


def test_all_three_stages_carry_the_flag():
    """Any one of them alone leaves a way for energy to reach the files."""
    assert '--no-energy"' in LIB_RS          # scan: stop computing it
    assert '--no-energy-keywords"' in LIB_RS  # tag-write: stop writing it
    assert '--no-energy-markers"' in LIB_RS   # markers-write: stop the cues


def test_the_frontend_sends_it_to_all_three():
    for cmd in ("scan_folder", "tag_videos", "write_markers"):
        call = re.search(rf'invoke<number>\("{cmd}", \{{[^}}]*\}}', MAIN_TS)
        assert call, f"{cmd} call site not found"
        assert "energy" in call.group(0), f"{cmd} does not pass the energy flag"


def test_the_default_stays_on_for_a_frontend_that_does_not_send_it():
    """Only the opt-out is transmitted, so an older webview keeps working."""
    for cmd in ("--no-energy", "--no-energy-keywords", "--no-energy-markers"):
        idx = LIB_RS.index(f'"{cmd}"')
        window = LIB_RS[max(0, idx - 200):idx]
        assert "energy == Some(false)" in window, f"{cmd} not gated on an explicit false"


def test_markers_write_gates_both_the_peak_lookup_and_the_events():
    """Two separate gates are needed and it is easy to add only one.

    The lookup decides which clips enter the marker set at all, so a clip
    pulled in only by its peaks must not be there. The per-clip events need
    their own gate because a clip can enter the set through its NAMED FACES
    and would otherwise still collect energy cues on the way past.
    """
    body = CLI_PY[CLI_PY.index("def markers_write"):]
    body = body[:body.index("\n@app.command")] if "\n@app.command" in body else body
    lookup = body[body.index("videos_with_energy_peaks") - 120:body.index("videos_with_energy_peaks")]
    assert "if energy_markers:" in lookup
    events = body[body.index("_energy_marker_events(conn, vid)") - 120:body.index("_energy_marker_events(conn, vid)")]
    assert "energy_markers" in events, "events are not gated on the flag"
    assert "energy_ok" in events, "events are not gated per clip"
