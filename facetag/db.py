"""SQLite-backed index for videos, faces, and people."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    duration_sec REAL,
    scanned_at TEXT DEFAULT CURRENT_TIMESTAMP,
    batch_tags TEXT
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp_sec REAL NOT NULL,
    bbox_x INTEGER, bbox_y INTEGER, bbox_w INTEGER, bbox_h INTEGER,
    embedding BLOB NOT NULL,
    cluster_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
CREATE INDEX IF NOT EXISTS idx_faces_video ON faces(video_id);

CREATE TABLE IF NOT EXISTS people (
    cluster_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place migrations for existing databases predating new columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "batch_tags" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN batch_tags TEXT")
    conn.commit()


def set_batch_tags(conn: sqlite3.Connection, video_id: int, tags: list[str]) -> None:
    """Store a comma-separated list of batch tags for a video.

    Tags are normalized to lowercase and de-duplicated.
    """
    clean = sorted({t.strip().lower() for t in tags if t and t.strip()})
    value = ",".join(clean) if clean else None
    conn.execute("UPDATE videos SET batch_tags = ? WHERE id = ?", (value, video_id))
    conn.commit()


def get_batch_tags(conn: sqlite3.Connection, video_id: int) -> list[str]:
    row = conn.execute("SELECT batch_tags FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row or not row[0]:
        return []
    return [t for t in row[0].split(",") if t]


def add_video(conn: sqlite3.Connection, path: str, duration_sec: float) -> int:
    cur = conn.execute(
        "INSERT INTO videos(path, duration_sec) VALUES (?, ?) "
        "ON CONFLICT(path) DO UPDATE SET duration_sec=excluded.duration_sec, scanned_at=CURRENT_TIMESTAMP "
        "RETURNING id",
        (path, duration_sec),
    )
    video_id = cur.fetchone()[0]
    conn.commit()
    return video_id


def is_scanned(conn: sqlite3.Connection, path: str) -> bool:
    row = conn.execute(
        "SELECT v.id FROM videos v WHERE v.path = ? "
        "AND EXISTS (SELECT 1 FROM faces f WHERE f.video_id = v.id) "
        "LIMIT 1",
        (path,),
    ).fetchone()
    return row is not None


def clear_video_faces(conn: sqlite3.Connection, video_id: int) -> None:
    conn.execute("DELETE FROM faces WHERE video_id = ?", (video_id,))
    conn.commit()


def add_faces_bulk(
    conn: sqlite3.Connection,
    video_id: int,
    rows: list[tuple[float, tuple[int, int, int, int], np.ndarray]],
) -> None:
    payload = [
        (video_id, t, x, y, w, h, emb.astype(np.float32).tobytes())
        for t, (x, y, w, h), emb in rows
    ]
    conn.executemany(
        "INSERT INTO faces(video_id, timestamp_sec, bbox_x, bbox_y, bbox_w, bbox_h, embedding) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()


def all_embeddings(conn: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    rows = conn.execute("SELECT id, embedding FROM faces").fetchall()
    if not rows:
        return [], np.empty((0, 512), dtype=np.float32)
    ids = [r[0] for r in rows]
    embs = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return ids, embs


def set_clusters(conn: sqlite3.Connection, assignments: dict[int, int]) -> None:
    conn.executemany(
        "UPDATE faces SET cluster_id = ? WHERE id = ?",
        [(cid, fid) for fid, cid in assignments.items()],
    )
    conn.commit()


def cluster_summary(conn: sqlite3.Connection) -> list[tuple[int, int, str | None]]:
    """Return (cluster_id, face_count, name?) for clusters with cluster_id IS NOT NULL, sorted by size desc."""
    rows = conn.execute(
        "SELECT f.cluster_id, COUNT(*) AS n, p.name "
        "FROM faces f LEFT JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE f.cluster_id IS NOT NULL AND f.cluster_id >= 0 "
        "GROUP BY f.cluster_id ORDER BY n DESC"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def name_cluster(conn: sqlite3.Connection, cluster_id: int, name: str) -> None:
    conn.execute(
        "INSERT INTO people(cluster_id, name) VALUES (?, ?) "
        "ON CONFLICT(cluster_id) DO UPDATE SET name=excluded.name",
        (cluster_id, name),
    )
    conn.commit()


def representative_faces(
    conn: sqlite3.Connection, cluster_id: int, n: int = 9
) -> list[tuple[str, float, tuple[int, int, int, int]]]:
    """Pick face samples spread across timestamps for visual labeling."""
    rows = conn.execute(
        "SELECT v.path, f.timestamp_sec, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h "
        "FROM faces f JOIN videos v ON v.id = f.video_id "
        "WHERE f.cluster_id = ? "
        "ORDER BY RANDOM() LIMIT ?",
        (cluster_id, n),
    ).fetchall()
    return [(r[0], r[1], (r[2], r[3], r[4], r[5])) for r in rows]


def videos_with_person(conn: sqlite3.Connection, name: str) -> list[tuple[int, str, int]]:
    rows = conn.execute(
        "SELECT v.id, v.path, COUNT(*) AS hits "
        "FROM faces f "
        "JOIN videos v ON v.id = f.video_id "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE p.name = ? "
        "GROUP BY v.id ORDER BY hits DESC",
        (name,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def face_times_in_video(
    conn: sqlite3.Connection, video_id: int, name: str
) -> list[float]:
    rows = conn.execute(
        "SELECT f.timestamp_sec FROM faces f "
        "JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE f.video_id = ? AND p.name = ? "
        "ORDER BY f.timestamp_sec",
        (video_id, name),
    ).fetchall()
    return [r[0] for r in rows]


def known_names(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT name FROM people ORDER BY name").fetchall()]


def has_clusters(conn: sqlite3.Connection) -> bool:
    """True if any face has been assigned to a cluster (i.e., a prior cluster
    run happened). Used to skip re-clustering by default, since re-clustering
    can re-shuffle cluster IDs and break already-saved cluster→name mappings.
    """
    row = conn.execute(
        "SELECT 1 FROM faces WHERE cluster_id IS NOT NULL AND cluster_id >= 0 LIMIT 1"
    ).fetchone()
    return row is not None
