"""Energy gets a confirm-before-write screen, like faces and matched tags.

v0.0.99 made energy declinable up front. That still left it as the one thing
Spotted decided about someone's footage without ever showing them: a user who
wanted energy had no way to see what it concluded, or to drop a level, before
it went into their files.

The case that made this nearly invisible: `activity-suggest` returns early when
the user typed no tags, and the frontend skipped the review whenever there were
no tag matches. Someone who skips the tags box still had every clip scored, and
is exactly who the review is for.
"""

from __future__ import annotations

import re
from pathlib import Path

from facetag import db as _db
from facetag import tag as _tag

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
CLI_PY = (ROOT / "facetag/cli.py").read_text()


def _library(tmp_path: Path):
    """Two clips scored differently, one of them also carrying a named face."""
    conn = _db.connect(tmp_path / "t.db")
    made = {}
    for name, bucket, score in (("loud.mov", "high", 0.9), ("calm.mov", "low", 0.1)):
        clip = tmp_path / name
        clip.write_bytes(b"")
        vid = _db.add_video(conn, str(clip), 2.0)
        conn.execute(
            "UPDATE videos SET energy_bucket=?, energy_score=?, energy_peaks=? WHERE id=?",
            (bucket, score, "[0.5]", vid),
        )
        made[name] = (vid, str(clip))
    conn.execute("INSERT INTO people(cluster_id,name) VALUES(3,'Ellie')")
    conn.execute(
        "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
        "VALUES(?,?,X'00',3)", (made["calm.mov"][0], 0.0),
    )
    conn.commit()
    return conn, made


def test_the_summary_groups_every_scored_clip_by_bucket(tmp_path):
    conn, made = _library(tmp_path)
    summary = _db.energy_bucket_summary(conn)
    assert summary["high"] == [made["loud.mov"][1]]
    assert summary["low"] == [made["calm.mov"][1]]


def test_unchecking_a_level_drops_only_that_level(tmp_path):
    conn, made = _library(tmp_path)
    kw = _tag.videos_with_keywords(conn, exclude_energy={"low"})
    assert "high energy" in kw[made["loud.mov"][1]]
    assert "low energy" not in kw[made["calm.mov"][1]]
    assert "Ellie" in kw[made["calm.mov"][1]], "dropping a level must not drop names"


def test_the_peak_cues_go_with_the_level_that_was_unchecked(tmp_path):
    """Unchecking "low energy" means no energy on those clips. Leaving their
    peak markers behind would be the same decision half-applied."""
    conn, made = _library(tmp_path)
    kept = dict(_db.videos_with_energy_peaks(conn, {"low"}))
    assert made["loud.mov"][0] in kept
    assert made["calm.mov"][0] not in kept


def test_a_clip_with_a_named_face_does_not_smuggle_its_cues_through():
    """calm.mov is in the marker set on its face alone, so filtering only the
    peak query would still leave its energy cues attached."""
    body = CLI_PY[CLI_PY.index("def markers_write"):]
    call = body.index("_energy_marker_events(conn, vid)")
    assert "energy_ok" in body[call - 120:call]


def test_energy_alone_is_enough_to_stop_and_ask():
    """The review used to be skipped whenever no tags matched, which is the
    normal state for someone who never typed any."""
    flow = MAIN_TS[MAIN_TS.index("async function startTagFlow"):]
    flow = flow[:flow.index("\n}")]
    assert "matched.length === 0 && energy.length === 0" in flow
    assert "renderReview(matched, energy)" in flow


def test_the_summary_is_emitted_even_when_the_user_typed_no_tags():
    """activity-suggest bails early in that case, and the emit has to happen
    before the bail or the review never sees it."""
    body = CLI_PY[CLI_PY.index("def activity_suggest"):]
    body = body[:body.index("\n@app.command")]
    bail = body.index("raise typer.Exit(0)")
    assert "_emit_energy_summary()" in body[:bail]


def test_a_batch_that_declined_scoring_is_not_asked_about_it():
    flow = MAIN_TS[MAIN_TS.index("async function startTagFlow"):]
    assert "energyEnabled ? parseEnergyBuckets(out) : []" in flow[:flow.index("\n}")]


def test_the_rejections_reach_both_write_stages():
    for cmd in ("tag_videos", "write_markers"):
        call = re.search(rf'invoke<number>\("{cmd}", \{{.*?\}}\)', MAIN_TS, re.S)
        assert call, f"{cmd} call site not found"
        assert "excludeEnergy" in call.group(0), f"{cmd} does not send the rejections"


def test_a_fresh_run_forgets_the_previous_runs_rejections():
    flow = MAIN_TS[MAIN_TS.index("async function startTagFlow"):]
    assert "excludedEnergyBuckets = []" in flow[:flow.index("\n}")]


def test_the_counter_and_toggle_cover_the_energy_rows_too():
    """A count that ignored them would read "0 of 2 will be written" while
    three energy levels sat checked underneath it."""
    fn = MAIN_TS[MAIN_TS.index("function renderReview"):]
    fn = fn[:fn.index("\n  screen.appendChild(wrap);")]
    assert "checks.concat(energyChecks)" in fn
    assert "energyChecks.forEach((c) => c.addEventListener" in fn
