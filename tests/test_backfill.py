"""Tests for the pre-CLIP embedding backfill in `scan`.

A library first scanned before activity detection shipped (v0.0.29) has faces
but zero frame embeddings. Because `scan` skips already-face-scanned videos,
those videos never get embeddings, so activity-suggest finds nothing for them
no matter how often the user re-runs. `scan` now backfills embeddings for such
videos (image encoder only — no face re-detection, no duplicate faces).

These use a fake encoder/detector so they run without the MobileCLIP model or
InsightFace, and stay fast.
"""
from __future__ import annotations

import numpy as np
import pytest

from facetag import cli, clip as _clip, db as _db, detect as _detect


class _FakeEncoder:
    """Stand-in for ClipEncoder: no model load, deterministic embedding."""

    available = True

    def _load(self) -> None:  # noqa: D401
        pass

    def encode_image(self, frame):
        v = np.zeros(512, dtype=np.float32)
        v[0] = 1.0
        return v


class _FakeDetector:
    """Stand-in for the InsightFace detector: finds nothing."""

    def __init__(self, **kwargs) -> None:
        pass

    def detect(self, frame):
        return []


def test_db_helpers_roundtrip(tmp_path):
    conn = _db.connect(tmp_path / "i.db")
    assert _db.video_id_for_path(conn, "/does/not/exist.mov") is None
    vid = _db.add_video(conn, "/a.mov", 1.0)
    assert _db.video_id_for_path(conn, "/a.mov") == vid
    assert _db.video_has_embeddings(conn, vid) is False
    _db.add_frame_embeddings_bulk(
        conn, vid, [(0.0, _clip.embedding_to_bytes(np.ones(512, dtype=np.float32)))]
    )
    assert _db.video_has_embeddings(conn, vid) is True


def _seed_preclip_video(conn, path_str: str) -> int:
    """Simulate a pre-activity-detection scan: one face, zero embeddings, and
    marked scan-complete — a real pre-CLIP library reaches that state via the
    migration backfill (which sets scan_complete=1 for clips that already have
    faces), and is_scanned now keys off that flag rather than face existence."""
    vid = _db.add_video(conn, path_str, 1.0)
    _db.add_faces_bulk(
        conn, vid, [(0.0, (1, 2, 3, 4), np.ones(512, dtype=np.float32))]
    )
    _db.mark_scan_complete(conn, vid)
    conn.commit()
    return vid


def test_scan_backfills_embeddings_for_preclip_video(tmp_path, test_mov, monkeypatch):
    monkeypatch.setattr(_clip, "ClipEncoder", _FakeEncoder)
    monkeypatch.setattr(_detect, "Detector", _FakeDetector)

    db_path = tmp_path / "i.db"
    conn = _db.connect(db_path)
    path_str = str(test_mov.resolve())
    vid = _seed_preclip_video(conn, path_str)
    assert _db.is_scanned(conn, path_str) is True
    assert _db.video_has_embeddings(conn, vid) is False
    faces_before = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE video_id=?", (vid,)
    ).fetchone()[0]
    conn.close()

    cli.scan(
        path=test_mov, db_path=db_path, sample_fps=2.0,
        rescan=False, min_score=0.5, tags="", activities=True,
    )

    conn = _db.connect(db_path)
    assert _db.video_has_embeddings(conn, vid) is True, "embeddings should be backfilled"
    faces_after = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE video_id=?", (vid,)
    ).fetchone()[0]
    assert faces_after == faces_before, "backfill must not re-detect or duplicate faces"
    conn.close()


def test_scan_skips_video_that_already_has_embeddings(tmp_path, test_mov, monkeypatch):
    monkeypatch.setattr(_clip, "ClipEncoder", _FakeEncoder)
    monkeypatch.setattr(_detect, "Detector", _FakeDetector)

    db_path = tmp_path / "i.db"
    conn = _db.connect(db_path)
    path_str = str(test_mov.resolve())
    vid = _seed_preclip_video(conn, path_str)
    _db.add_frame_embeddings_bulk(
        conn, vid, [(0.0, _clip.embedding_to_bytes(np.ones(512, dtype=np.float32)))]
    )
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM frame_embeddings WHERE video_id=?", (vid,)
    ).fetchone()[0]
    conn.close()

    cli.scan(
        path=test_mov, db_path=db_path, sample_fps=2.0,
        rescan=False, min_score=0.5, tags="", activities=True,
    )

    conn = _db.connect(db_path)
    after = conn.execute(
        "SELECT COUNT(*) FROM frame_embeddings WHERE video_id=?", (vid,)
    ).fetchone()[0]
    assert after == before, "idempotent: a video with embeddings must not be re-backfilled"
    conn.close()


def test_scan_skips_when_activities_disabled(tmp_path, test_mov, monkeypatch):
    """With --no-activities there's no encoder, so a pre-CLIP video is skipped
    (not backfilled) — matching the prior behavior when the user opts out."""
    monkeypatch.setattr(_detect, "Detector", _FakeDetector)

    db_path = tmp_path / "i.db"
    conn = _db.connect(db_path)
    path_str = str(test_mov.resolve())
    vid = _seed_preclip_video(conn, path_str)
    conn.close()

    cli.scan(
        path=test_mov, db_path=db_path, sample_fps=2.0,
        rescan=False, min_score=0.5, tags="", activities=False,
    )

    conn = _db.connect(db_path)
    assert _db.video_has_embeddings(conn, vid) is False
    conn.close()
