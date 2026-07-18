"""Tests for per-clip energy scoring and its wiring into tags + markers.

Pure-function tests run everywhere. The end-to-end score_clip and marker
round-trip are guarded on ffmpeg/exiftool like the other metadata tests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from facetag import db as _db
from facetag import energy as _energy
from facetag import markers as _markers
from facetag import tag as _tag


# --- pure functions --------------------------------------------------------
def test_bucket_thresholds():
    assert _energy.bucket_for(0.9) == "high"
    assert _energy.bucket_for(_energy.BUCKET_HIGH) == "high"
    assert _energy.bucket_for(0.0) == "low"
    assert _energy.bucket_for(_energy.BUCKET_LOW) == "low"
    mid = (_energy.BUCKET_LOW + _energy.BUCKET_HIGH) / 2
    assert _energy.bucket_for(mid) == "medium"


def test_audio01_maps_floor_and_ceiling():
    a = _energy._audio01(np.array(
        [_energy.AUDIO_FLOOR_DBFS, _energy.AUDIO_CEIL_DBFS, -200.0, 0.0],
        dtype=np.float32))
    assert a[0] == pytest.approx(0.0)
    assert a[1] == pytest.approx(1.0)
    assert a[2] == pytest.approx(0.0)   # far below floor clamps to 0
    assert a[3] == pytest.approx(1.0)   # above ceiling clamps to 1


def test_motion01_monotonic_and_clamped():
    m = _energy._motion01(np.array(
        [0.0, _energy.MOTION_FLOOR, 4.0, _energy.MOTION_CEIL, 999.0],
        dtype=np.float32))
    assert m[0] == pytest.approx(0.0)
    assert m[1] == pytest.approx(0.0)
    assert m[3] == pytest.approx(1.0)
    assert m[4] == pytest.approx(1.0)
    assert m[1] <= m[2] <= m[3]         # monotonic across the range


def test_find_peaks_respects_min_gap_and_threshold():
    # peaks at idx 1 (0.6) and 5 (0.7); idx 3 (0.55) is inside the gap of a
    # higher neighbor and should be dropped.
    series = np.array([0.1, 0.6, 0.2, 0.55, 0.2, 0.7, 0.1], dtype=np.float32)
    peaks = _energy._find_peaks(series)
    assert peaks == [1.0, 5.0]


def test_find_peaks_empty_when_all_below_min():
    series = np.array([0.1, 0.2, 0.3, 0.1], dtype=np.float32)
    assert _energy._find_peaks(series) == []


def test_energy_result_keyword():
    r = _energy.EnergyResult(score=0.9, bucket="high", series=np.ones(3))
    assert r.keyword == "high energy"


# --- DB + tag wiring (no ffmpeg needed) ------------------------------------
def test_set_energy_and_keyword_flows_to_tag_write(tmp_path: Path):
    conn = _db.connect(tmp_path / "index.db")
    vid = _db.add_video(conn, "/clips/party.mov", 12.0)
    _db.set_energy(conn, vid, score=0.8, bucket="high", peaks=[3.0, 7.5])

    assert _db.video_has_energy(conn, vid) is True
    assert _db.videos_with_energy(conn) == {"/clips/party.mov": "high"}
    assert _db.energy_peaks_for_video(conn, vid) == [3.0, 7.5]
    assert _db.videos_with_energy_peaks(conn) == [(vid, "/clips/party.mov")]

    # A clip with only an energy reading (no faces / activity tags) still gets
    # its "<bucket> energy" keyword — that's the "by default" behavior.
    kw = _tag.videos_with_keywords(conn)
    assert kw["/clips/party.mov"] == ["high energy"]


def test_energy_keyword_can_be_excluded(tmp_path: Path):
    conn = _db.connect(tmp_path / "index.db")
    vid = _db.add_video(conn, "/clips/quiet.mov", 5.0)
    _db.set_energy(conn, vid, score=0.1, bucket="low", peaks=[])
    kw = _tag.videos_with_keywords(conn, exclude_tags={"low energy"})
    assert "/clips/quiet.mov" not in kw       # nothing else to tag, so dropped


def test_no_energy_no_peaks(tmp_path: Path):
    conn = _db.connect(tmp_path / "index.db")
    vid = _db.add_video(conn, "/clips/plain.mov", 5.0)
    assert _db.video_has_energy(conn, vid) is False
    assert _db.videos_with_energy_peaks(conn) == []
    assert _db.energy_peaks_for_video(conn, vid) == []


# --- end-to-end: score a real generated clip -------------------------------
def test_score_clip_on_generated_clip(tmp_path: Path, have_ffmpeg: bool):
    if not have_ffmpeg:
        pytest.skip("ffmpeg not on PATH")
    clip = tmp_path / "gen.mp4"
    subprocess.check_call(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-pix_fmt", "yuv420p", "-shortest", str(clip)],
        stderr=subprocess.DEVNULL,
    )
    res = _energy.score_clip(clip)
    assert res.bucket in _energy.BUCKETS
    assert res.series.size > 0
    assert res.have_audio is True          # sine tone present
    assert 0.0 <= res.score <= 1.0


# --- marker round-trip: energy peaks become timeline cues ------------------
def test_energy_peaks_write_as_markers(test_mov: Path, have_exiftool: bool):
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    events = [(0.5, "Energy peak"), (1.0, "Energy peak")]
    _markers.write_markers(test_mov, events)
    raw = _markers.read_markers(test_mov)
    assert "Energy peak" in raw
