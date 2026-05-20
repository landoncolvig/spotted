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

CREATE TABLE IF NOT EXISTS hidden_clusters (
    cluster_id INTEGER PRIMARY KEY
);
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
    # hidden_clusters table is created by SCHEMA's IF NOT EXISTS — no migration needed.
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


def unclustered_embeddings(conn: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    """Faces with cluster_id IS NULL — the ones a fresh scan just added.

    Used by the incremental cluster pass so dropping a new clip into a
    library that already has labeled clusters doesn't reshuffle existing
    cluster IDs (which would break previously saved labels).
    """
    rows = conn.execute(
        "SELECT id, embedding FROM faces WHERE cluster_id IS NULL"
    ).fetchall()
    if not rows:
        return [], np.empty((0, 512), dtype=np.float32)
    ids = [r[0] for r in rows]
    embs = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return ids, embs


def cluster_centroids(conn: sqlite3.Connection) -> dict[int, np.ndarray]:
    """Return {cluster_id: mean_embedding} for every non-noise cluster.

    Used by incremental clustering to assign new faces to the nearest
    existing person, inheriting any saved label automatically.
    """
    rows = conn.execute(
        "SELECT cluster_id, embedding FROM faces "
        "WHERE cluster_id IS NOT NULL AND cluster_id >= 0"
    ).fetchall()
    by_cluster: dict[int, list[np.ndarray]] = {}
    for cid, blob in rows:
        by_cluster.setdefault(cid, []).append(np.frombuffer(blob, dtype=np.float32))
    return {cid: np.mean(np.stack(es), axis=0) for cid, es in by_cluster.items()}


def max_cluster_id(conn: sqlite3.Connection) -> int:
    """Highest non-noise cluster_id currently in use, or -1 if none."""
    row = conn.execute(
        "SELECT COALESCE(MAX(cluster_id), -1) "
        "FROM faces WHERE cluster_id IS NOT NULL AND cluster_id >= 0"
    ).fetchone()
    return int(row[0]) if row else -1


def set_clusters(conn: sqlite3.Connection, assignments: dict[int, int]) -> None:
    conn.executemany(
        "UPDATE faces SET cluster_id = ? WHERE id = ?",
        [(cid, fid) for fid, cid in assignments.items()],
    )
    conn.commit()


def cluster_summary(conn: sqlite3.Connection, include_hidden: bool = False) -> list[tuple[int, int, str | None]]:
    """Return (cluster_id, face_count, name?) for clusters with cluster_id IS NOT NULL, sorted by size desc.

    By default skips clusters the user has hidden via the labeler "X" button.
    """
    rows = conn.execute(
        "SELECT f.cluster_id, COUNT(*) AS n, p.name "
        "FROM faces f LEFT JOIN people p ON p.cluster_id = f.cluster_id "
        "WHERE f.cluster_id IS NOT NULL AND f.cluster_id >= 0 "
        "GROUP BY f.cluster_id ORDER BY n DESC"
    ).fetchall()
    out = [(r[0], r[1], r[2]) for r in rows]
    if include_hidden:
        return out
    hidden = hidden_cluster_ids(conn)
    return [row for row in out if row[0] not in hidden]


def cluster_summary_with_videos(
    conn: sqlite3.Connection,
    *,
    include_hidden: bool = False,
    scope_paths: list[str] | None = None,
) -> list[tuple[int, int, str | None, list[str]]]:
    """Return (cluster_id, face_count, name?, video_paths) per cluster.

    `video_paths` is the full sorted list of videos that contain at least
    one face in this cluster — the labeler uses it to display per-card
    "seen in" hints so a user can tell which footage each unnamed cluster
    came from before naming it.

    `scope_paths`, if given, filters to clusters that include at least one
    face from a video whose absolute path equals any scope path OR sits
    inside a scope directory (prefix match on `<scope>/`). This lets the
    labeler focus on "just the clip(s) I just dropped" instead of every
    unnamed cluster in the library — the original sin that caused users
    to label faces from old batches by mistake.
    """
    base = cluster_summary(conn, include_hidden=include_hidden)
    if not base:
        return []

    cids = [c[0] for c in base]
    placeholders = ",".join(["?"] * len(cids))
    rows = conn.execute(
        f"SELECT f.cluster_id, v.path "
        f"FROM faces f JOIN videos v ON v.id = f.video_id "
        f"WHERE f.cluster_id IN ({placeholders}) "
        f"GROUP BY f.cluster_id, v.path "
        f"ORDER BY f.cluster_id, v.path",
        cids,
    ).fetchall()
    by_cluster: dict[int, list[str]] = {}
    for cid, vp in rows:
        by_cluster.setdefault(cid, []).append(vp)

    def _in_scope(video_path: str, scopes: list[str]) -> bool:
        for s in scopes:
            if video_path == s or video_path.startswith(s.rstrip("/") + "/"):
                return True
        return False

    if scope_paths:
        # Resolve each scope path so a "~/Downloads" arg matches the
        # absolute path stored in `videos.path`.
        import os
        scopes = [os.path.realpath(os.path.expanduser(p)) for p in scope_paths]
        out: list[tuple[int, int, str | None, list[str]]] = []
        for cid, count, name in base:
            vids = by_cluster.get(cid, [])
            if any(_in_scope(v, scopes) for v in vids):
                out.append((cid, count, name, vids))
        return out

    return [(cid, count, name, by_cluster.get(cid, [])) for cid, count, name in base]


def name_cluster(conn: sqlite3.Connection, cluster_id: int, name: str) -> None:
    conn.execute(
        "INSERT INTO people(cluster_id, name) VALUES (?, ?) "
        "ON CONFLICT(cluster_id) DO UPDATE SET name=excluded.name",
        (cluster_id, name),
    )
    conn.commit()


def representative_faces(
    conn: sqlite3.Connection,
    cluster_id: int,
    n: int = 9,
    scope_paths: list[str] | None = None,
) -> list[tuple[str, float, tuple[int, int, int, int]]]:
    """Pick face samples spread across timestamps for visual labeling.

    If `scope_paths` is provided, samples only from videos whose path
    matches a scope path (file equality) or sits under a scope dir
    (prefix match). This is what the labeler uses when scoped to a
    just-dropped clip: cluster IDs are library-wide (a face in cluster 8
    might come from this clip AND from older footage), but the thumbnail
    should show faces FROM the clip the user just dropped, not stale
    crops from the original batch that first formed the cluster.

    Falls back to library-wide sampling if `scope_paths` is set but no
    in-scope faces exist for that cluster.
    """
    def _query(where_extra: str, params: tuple):
        return conn.execute(
            "SELECT v.path, f.timestamp_sec, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h "
            "FROM faces f JOIN videos v ON v.id = f.video_id "
            f"WHERE f.cluster_id = ? {where_extra} "
            "ORDER BY RANDOM() LIMIT ?",
            params,
        ).fetchall()

    if scope_paths:
        import os
        scopes = [os.path.realpath(os.path.expanduser(p)) for p in scope_paths]
        # Build OR'd predicate: v.path = scope OR v.path LIKE 'scope/%'
        clauses = []
        params: list = [cluster_id]
        for s in scopes:
            s = s.rstrip("/")
            clauses.append("(v.path = ? OR v.path LIKE ?)")
            params.extend([s, s + "/%"])
        where_extra = "AND (" + " OR ".join(clauses) + ")"
        params.append(n)
        scoped = _query(where_extra, tuple(params))
        if scoped:
            return [(r[0], r[1], (r[2], r[3], r[4], r[5])) for r in scoped]

    rows = _query("", (cluster_id, n))
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


def hide_cluster(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Mark a cluster as hidden so it doesn't appear in the labeler anymore."""
    conn.execute(
        "INSERT OR IGNORE INTO hidden_clusters(cluster_id) VALUES (?)", (cluster_id,)
    )
    # Also drop any name on it — hiding implies it's not a person.
    conn.execute("DELETE FROM people WHERE cluster_id = ?", (cluster_id,))
    conn.commit()


def unhide_cluster(conn: sqlite3.Connection, cluster_id: int) -> None:
    conn.execute("DELETE FROM hidden_clusters WHERE cluster_id = ?", (cluster_id,))
    conn.commit()


def hidden_cluster_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT cluster_id FROM hidden_clusters").fetchall()
    return {r[0] for r in rows}


def merge_clusters_by_name(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """Consolidate all clusters sharing a name into one canonical cluster
    per name. Returns {name: [merged_cluster_ids_in_size_order]}.

    Strategy: for each name with 2+ clusters, pick the cluster with the
    most faces as canonical. Re-point all faces from the other clusters
    to the canonical cluster_id. Delete the redundant people rows.

    Idempotent — running it twice is a no-op.
    """
    rows = conn.execute(
        "SELECT p.cluster_id, p.name, COUNT(f.id) AS cnt "
        "FROM people p "
        "LEFT JOIN faces f ON f.cluster_id = p.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != '' "
        "GROUP BY p.cluster_id, p.name "
        "ORDER BY p.name, cnt DESC"
    ).fetchall()

    by_name: dict[str, list[tuple[int, int]]] = {}
    for cid, name, cnt in rows:
        by_name.setdefault(name, []).append((int(cid), int(cnt)))

    merged: dict[str, list[int]] = {}
    for name, entries in by_name.items():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda x: -x[1])  # largest cluster first
        canonical = entries[0][0]
        others = [cid for cid, _ in entries[1:]]
        for old_cid in others:
            conn.execute(
                "UPDATE faces SET cluster_id = ? WHERE cluster_id = ?",
                (canonical, old_cid),
            )
            conn.execute("DELETE FROM people WHERE cluster_id = ?", (old_cid,))
        merged[name] = [cid for cid, _ in entries]

    conn.commit()
    return merged
