"""Integration tests for the timeline-markers write path.

Covers the in-file XMP-xmpDM:Markers write and the sidecar .mov.xmp
write that v0.0.28 added (DaVinci's in-file XMP marker reading was
inconsistent, so we ship the sidecar as a fallback that DaVinci's
project-level XMP import picks up reliably).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from facetag import db as _db
from facetag import markers as _markers


def _seed_named_clip(conn, path: str, faces: list[tuple[float, int, str | None]]) -> int:
    """Insert a video plus faces. faces = [(timestamp_sec, cluster_id, name?)].
    A name of None inserts the face but no people row (an unnamed cluster)."""
    vid = _db.add_video(conn, path, 5.0)
    emb = np.ones(512, dtype=np.float32).tobytes()
    for t, cid, name in faces:
        conn.execute(
            "INSERT INTO faces(video_id, timestamp_sec, bbox_x, bbox_y, bbox_w, bbox_h, embedding, cluster_id) "
            "VALUES (?, ?, 0, 0, 1, 1, ?, ?)",
            (vid, t, emb, cid),
        )
        if name is not None:
            conn.execute(
                "INSERT OR IGNORE INTO people(cluster_id, name) VALUES (?, ?)", (cid, name)
            )
    conn.commit()
    return vid


def test_resolve_manifest_groups_and_dedupes_named_faces(tmp_path: Path) -> None:
    conn = _db.connect(tmp_path / "i.db")
    _seed_named_clip(conn, "/a.mov", [(0.5, 1, "Sarah"), (3.0, 2, "Tom"), (0.5, 1, "Sarah")])
    _seed_named_clip(conn, "/b.mov", [(1.0, 3, None)])  # only an unnamed cluster
    m = _markers.build_resolve_manifest(conn)
    assert m["schema"] == _markers.RESOLVE_MANIFEST_SCHEMA
    assert "/a.mov" in m["clips"]
    names = [e["name"] for e in m["clips"]["/a.mov"]]
    assert names.count("Sarah") == 1, "duplicate (time, name) should collapse to one marker"
    assert "Tom" in names
    assert "/b.mov" not in m["clips"], "clips with no named faces are excluded"


def test_write_resolve_manifest_writes_json(tmp_path: Path) -> None:
    conn = _db.connect(tmp_path / "i.db")
    _seed_named_clip(conn, "/a.mov", [(0.5, 1, "Sarah")])
    out = tmp_path / "manifest.json"
    n = _markers.write_resolve_manifest(conn, out)
    assert n == 1
    assert out.is_file()
    data = json.loads(out.read_text())
    assert data["schema"] == _markers.RESOLVE_MANIFEST_SCHEMA
    assert data["clips"]["/a.mov"][0]["name"] == "Sarah"
    assert data["clips"]["/a.mov"][0]["t"] == 0.5


def test_write_markers_in_file_roundtrip(test_mov: Path, have_exiftool: bool) -> None:
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    events = [(0.5, "Sarah"), (1.2, "Tom"), (0.5, "Sarah")]  # dup gets deduped
    _markers.write_markers(test_mov, events)
    raw = _markers.read_markers(test_mov)
    assert raw, "expected exiftool to read back non-empty Markers"
    # Both names should appear; dedup means each marker appears once
    assert raw.count("Name=Sarah") == 1
    assert raw.count("Name=Tom") == 1


def test_write_markers_sidecar_creates_file(test_mov: Path, have_exiftool: bool) -> None:
    """v0.0.28 added a sidecar `<clip>.mov.xmp` next to each clip because
    DaVinci's project-level XMP import reads sidecars more reliably than
    in-file XMP across versions. This test guards that path."""
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    events = [(0.5, "Sarah"), (1.2, "Tom")]
    sidecar = _markers.write_markers_sidecar(test_mov, events)
    assert sidecar is not None
    assert sidecar.is_file()
    assert sidecar.name == "test.mov.xmp"
    raw = _markers.read_markers_sidecar(test_mov)
    assert raw, "expected sidecar XMP to contain markers"
    assert "Name=Sarah" in raw
    assert "Name=Tom" in raw


def test_write_markers_empty_events_returns_none(test_mov: Path) -> None:
    """No face events → don't waste an exiftool call; return None."""
    assert _markers.write_markers_sidecar(test_mov, []) is None
    # In-file write should also be a no-op (returns None implicitly)
    _markers.write_markers(test_mov, [])


def test_write_markers_sidecar_is_idempotent(test_mov: Path, have_exiftool: bool) -> None:
    """Re-running on the same clip wipes the previous sidecar (exiftool's
    -o refuses to overwrite, so write_markers_sidecar unlinks first) and
    writes fresh. Re-tagging a folder shouldn't fail with 'file exists'."""
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    events_a = [(0.5, "Sarah")]
    events_b = [(0.7, "Tom"), (1.2, "Ellie")]
    _markers.write_markers_sidecar(test_mov, events_a)
    _markers.write_markers_sidecar(test_mov, events_b)
    raw = _markers.read_markers_sidecar(test_mov)
    assert "Name=Tom" in raw
    assert "Name=Ellie" in raw
    assert "Name=Sarah" not in raw, "stale marker from first call leaked through"
