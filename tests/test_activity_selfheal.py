"""activity-suggest self-heals missing frame embeddings instead of blanket-
stamping every typed tag onto every clip.

The discriminator between the two paths is whether embeddings get written:
- self-heal computes embeddings on demand, then matches per clip;
- the old fallback (kept only for a genuinely unloadable model) writes none.
"""
from __future__ import annotations

import numpy as np
import pytest
from typer.testing import CliRunner

import facetag.clip as _clip
from facetag import cli as _cli
from facetag import db as _db

runner = CliRunner()


class FakeEncoder:
    """Loads fine; returns deterministic normalized 512-d vectors."""
    available = True

    def _load(self):
        pass

    def encode_image(self, frame):
        v = np.full(512, 0.5, dtype=np.float32)
        return v / np.linalg.norm(v)

    def encode_texts(self, texts):
        m = np.full((len(texts), 512), 0.5, dtype=np.float32)
        return m / np.linalg.norm(m, axis=1, keepdims=True)


class UnavailableEncoder:
    available = False

    def _load(self):
        raise _clip.ClipUnavailable("test: scene model missing")


def _seed(db_path, video_path, tags):
    conn = _db.connect(db_path)
    vid = _db.add_video(conn, str(video_path), 1.0)
    _db.set_batch_tags(conn, vid, tags)
    conn.commit()
    conn.close()
    return vid


def test_self_heal_computes_embeddings(tmp_path, test_mov, monkeypatch):
    db_path = tmp_path / "index.db"
    vid = _seed(db_path, test_mov, ["beach", "wedding"])
    monkeypatch.setattr(_clip, "ClipEncoder", FakeEncoder)

    result = runner.invoke(_cli.app, ["activity-suggest", "--db", str(db_path), "--threshold", "0.1"])
    assert result.exit_code == 0, result.output

    conn = _db.connect(db_path)
    # The load-bearing assertion: the clip that had NO embeddings now has them,
    # which only the self-heal path does. The old blanket-stamp wrote none.
    assert _db.video_has_embeddings(conn, vid) is True


def test_unavailable_model_still_blanket_stamps(tmp_path, test_mov, monkeypatch):
    db_path = tmp_path / "index.db"
    vid = _seed(db_path, test_mov, ["beach", "wedding"])
    monkeypatch.setattr(_clip, "ClipEncoder", UnavailableEncoder)

    result = runner.invoke(_cli.app, ["activity-suggest", "--db", str(db_path), "--threshold", "0.1"])
    assert result.exit_code == 0, result.output

    conn = _db.connect(db_path)
    # Genuine model-unavailable: fall back to preserving the user's tags on every
    # clip (deliberate), and crucially it did NOT fabricate embeddings.
    tags = {t for (t,) in conn.execute("SELECT tag FROM auto_tags WHERE video_id = ?", (vid,)).fetchall()}
    assert tags == {"beach", "wedding"}
    assert _db.video_has_embeddings(conn, vid) is False


def test_no_files_on_disk_no_garbage(tmp_path, monkeypatch):
    # Indexed clip whose file does not exist: model loads, but there's nothing to
    # analyze. Must NOT blanket-stamp garbage — exits clean with nothing tagged.
    db_path = tmp_path / "index.db"
    vid = _seed(db_path, tmp_path / "missing.mov", ["beach", "wedding"])
    monkeypatch.setattr(_clip, "ClipEncoder", FakeEncoder)

    result = runner.invoke(_cli.app, ["activity-suggest", "--db", str(db_path), "--threshold", "0.1"])
    assert result.exit_code == 0, result.output

    conn = _db.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM auto_tags WHERE video_id = ?", (vid,)).fetchone()[0]
    assert n == 0
    assert _db.video_has_embeddings(conn, vid) is False
