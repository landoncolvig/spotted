"""SQLite-backed index for videos, faces, and people."""
from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hidden_clusters (
    cluster_id INTEGER PRIMARY KEY
);

-- MobileCLIP image-encoder output per sampled frame. One row per scan-time
-- frame so the activity-suggest step can score curated text prompts against
-- the whole library without re-running the encoder.
CREATE TABLE IF NOT EXISTS frame_embeddings (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    timestamp_sec REAL NOT NULL,
    embedding BLOB NOT NULL  -- float16, 512 dims, 1024 bytes per row
);
CREATE INDEX IF NOT EXISTS idx_frame_emb_video ON frame_embeddings(video_id);

-- Auto-applied activity tags (one row per (video, tag)) produced by the
-- MobileCLIP-driven activity-suggest step. Kept separate from batch_tags
-- so we can distinguish auto from manual and remove all autos in one shot
-- on re-classification.
CREATE TABLE IF NOT EXISTS auto_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (video_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_auto_tags_video ON auto_tags(video_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: the labeling server stays alive across batches by design, so it
    # genuinely contends with scan/tag-write running as separate processes. At
    # the default 5s a name typed during a commit raised "database is locked",
    # the labeler card showed "x retry" and the typed name was lost.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the labeler read while a scan writes instead of blocking.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass  # a read-only or unusual filesystem; the default journal still works
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """In-place migrations for existing databases predating new columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "batch_tags" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN batch_tags TEXT")
    if "spotted_keywords" not in cols:
        # Newline-joined record of the exact keyword set Spotted last wrote into
        # this clip's file. Lets tag-write replace only its OWN prior keywords
        # (so renames/removals take effect) while preserving keywords another
        # tool (Premiere, Photos, Bridge) put in the file.
        conn.execute("ALTER TABLE videos ADD COLUMN spotted_keywords TEXT")
    if "energy_bucket" not in cols:
        # Per-clip "energy" (excitement) computed at scan time from audio
        # loudness + camera-compensated motion. energy_bucket is high|medium|low
        # (written as the "<bucket> energy" keyword); energy_score is the raw
        # 0..1 aggregate; energy_peaks is a JSON list of peak timestamps (sec)
        # turned into timeline markers.
        conn.execute("ALTER TABLE videos ADD COLUMN energy_score REAL")
        conn.execute("ALTER TABLE videos ADD COLUMN energy_bucket TEXT")
        conn.execute("ALTER TABLE videos ADD COLUMN energy_peaks TEXT")
    if "energy_version" not in cols:
        # Energy results persist across app updates. A version lets scan refresh
        # old rows when peak selection or scoring changes, instead of treating
        # a pre-update result as current forever.
        conn.execute("ALTER TABLE videos ADD COLUMN energy_version INTEGER")
    if "scan_complete" not in cols:
        # A reliable "this clip finished scanning" flag. is_scanned used to infer
        # it from "has >=1 face row", which mis-fires both ways: a clip that
        # crashed mid-scan looked done (and got skipped forever, losing the rest
        # of its faces/embeddings), and a legitimately face-less clip never
        # looked done (so it re-embedded on every re-drop). Backfill 1 for clips
        # that already have faces so existing libraries aren't force-rescanned.
        conn.execute("ALTER TABLE videos ADD COLUMN scan_complete INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE videos SET scan_complete = 1 "
            "WHERE EXISTS (SELECT 1 FROM faces f WHERE f.video_id = videos.id)"
        )
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


def get_spotted_keywords(conn: sqlite3.Connection, video_id: int) -> list[str]:
    """The exact keyword set Spotted last wrote into this clip's file (or [])."""
    row = conn.execute(
        "SELECT spotted_keywords FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    return [k for k in row[0].split("\n") if k]


def set_spotted_keywords(
    conn: sqlite3.Connection, video_id: int, keywords: list[str]
) -> None:
    """Record what Spotted just wrote, so the next write can replace only its
    own keywords. Newline-joined because keywords/person names may contain commas."""
    clean = [k for k in keywords if k and k.strip()]
    value = "\n".join(clean) if clean else None
    conn.execute(
        "UPDATE videos SET spotted_keywords = ? WHERE id = ?", (value, video_id)
    )
    conn.commit()


def set_energy(
    conn: sqlite3.Connection,
    video_id: int,
    score: float,
    bucket: str,
    peaks: list[float],
    *,
    version: int | None = None,
) -> None:
    """Store a clip's energy score, bucket, and peak timestamps (JSON)."""
    conn.execute(
        "UPDATE videos SET energy_score = ?, energy_bucket = ?, energy_peaks = ?, "
        "energy_version = ? WHERE id = ?",
        (
            float(score),
            bucket,
            json.dumps([round(float(p), 3) for p in peaks]),
            version,
            video_id,
        ),
    )
    conn.commit()


def video_has_energy(conn: sqlite3.Connection, video_id: int) -> bool:
    row = conn.execute(
        "SELECT energy_bucket FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    return bool(row and row[0])


def video_has_current_energy(
    conn: sqlite3.Connection, video_id: int, version: int
) -> bool:
    row = conn.execute(
        "SELECT energy_bucket, energy_version FROM videos WHERE id = ?",
        (video_id,),
    ).fetchone()
    return bool(row and row[0] and row[1] == version)


def videos_with_energy(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {video_path: energy_bucket} for every clip that has one scored."""
    rows = conn.execute(
        "SELECT path, energy_bucket FROM videos WHERE energy_bucket IS NOT NULL AND energy_bucket != ''"
    ).fetchall()
    return {p: b for p, b in rows}


def energy_peaks_for_video(conn: sqlite3.Connection, video_id: int) -> list[float]:
    row = conn.execute(
        "SELECT energy_peaks FROM videos WHERE id = ?", (video_id,)
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        return [float(t) for t in json.loads(row[0])]
    except (ValueError, TypeError):
        return []


def videos_with_energy_peaks(
    conn: sqlite3.Connection,
    exclude_buckets: set[str] | None = None,
) -> list[tuple[int, str]]:
    """Return [(video_id, path)] for clips that have at least one energy peak.

    `exclude_buckets` drops the buckets a user unchecked on the review screen.
    Unchecking "low energy" means they do not want energy on those clips, so
    the peak cues go with the keyword rather than surviving it.
    """
    rows = conn.execute(
        "SELECT id, path, energy_bucket FROM videos WHERE energy_peaks IS NOT NULL "
        "AND energy_peaks != '' AND energy_peaks != '[]'"
    ).fetchall()
    drop = {b.strip().lower() for b in (exclude_buckets or set()) if b.strip()}
    return [
        (int(i), p) for i, p, bucket in rows
        if (bucket or "").strip().lower() not in drop
    ]


def energy_bucket_summary(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """{bucket: [clip paths]} for every scored clip, busiest bucket first.

    Feeds the review screen. Energy is the last thing Spotted decided about
    someone's footage without showing them first.
    """
    rows = conn.execute(
        "SELECT energy_bucket, path FROM videos "
        "WHERE energy_bucket IS NOT NULL AND energy_bucket != '' "
        "ORDER BY energy_score DESC"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for bucket, path in rows:
        out.setdefault(bucket, []).append(path)
    return out


def _split_batch_tag_rows(rows) -> list[str]:
    seen: set[str] = set()
    for (csv,) in rows:
        for t in csv.split(","):
            t = t.strip()
            if t:
                seen.add(t)
    return sorted(seen)


def all_batch_tags(
    conn: sqlite3.Connection, scope_root: str | list[str] | None = None
) -> list[str]:
    """Distinct batch tags, optionally only for clips under `scope_root`.

    `scope_root` may be one path or a list of them: a batch is whatever the
    user dropped, and a Finder multi-selection is many paths.

    These are the tags the user typed on the welcome screen. The activity
    matcher uses them as its whole vocabulary: it looks for each of these
    tags per clip (via CLIP) and applies it only where it actually appears,
    the same way face names attach only to clips a person is in.

    Scoping matters: unioned across the whole library, a new batch gets
    searched for words the user typed months ago for entirely different
    footage. "conference room" and "sticky notes" turned up on a tester's
    Vegas trip that way.
    """
    if scope_root:
        roots = [scope_root] if isinstance(scope_root, str) else list(scope_root)
        roots = [r.rstrip("/") for r in roots if r]
        clause = " OR ".join(["(path = ? OR path LIKE ? || '/%')"] * len(roots))
        params: list[str] = []
        for r in roots:
            params += [r, r]
        rows = conn.execute(
            "SELECT DISTINCT batch_tags FROM videos "
            "WHERE batch_tags IS NOT NULL AND batch_tags != '' "
            f"AND ({clause})",
            params,
        ).fetchall()
        if rows:
            return _split_batch_tag_rows(rows)
        # fall through: nothing recorded for this folder yet
    rows = conn.execute(
        "SELECT DISTINCT batch_tags FROM videos WHERE batch_tags IS NOT NULL AND batch_tags != ''"
    ).fetchall()
    seen: set[str] = set()
    for (csv,) in rows:
        for t in csv.split(","):
            t = t.strip()
            if t:
                seen.add(t)
    return sorted(seen)


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
    """True only if this clip finished a full scan (see the scan_complete flag).

    Keyed off scan_complete rather than face existence so a clip that crashed
    mid-scan is re-scanned (not skipped forever) and a face-less clip counts as
    done (not re-embedded on every re-drop)."""
    row = conn.execute(
        "SELECT 1 FROM videos WHERE path = ? AND scan_complete = 1 LIMIT 1",
        (path,),
    ).fetchone()
    return row is not None


def mark_scan_complete(conn: sqlite3.Connection, video_id: int) -> None:
    """Flag a clip as fully scanned — called only after its faces, embeddings,
    and energy have all been written, so a partial/crashed scan never sticks."""
    conn.execute("UPDATE videos SET scan_complete = 1 WHERE id = ?", (video_id,))
    conn.commit()


def video_id_for_path(conn: sqlite3.Connection, path: str) -> int | None:
    """Look up an existing video's id by path, or None if not indexed."""
    row = conn.execute("SELECT id FROM videos WHERE path = ?", (path,)).fetchone()
    return int(row[0]) if row else None


def video_has_embeddings(conn: sqlite3.Connection, video_id: int) -> bool:
    """True if the video already has at least one frame embedding.

    Used by `scan` to decide whether an already-face-scanned video still
    needs an activity-embedding backfill. Libraries first scanned before
    activity detection shipped have faces but zero embeddings; without a
    backfill, activity-suggest finds nothing for them forever.
    """
    row = conn.execute(
        "SELECT 1 FROM frame_embeddings WHERE video_id = ? LIMIT 1", (video_id,)
    ).fetchone()
    return row is not None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def mark_scan_incomplete(conn: sqlite3.Connection, video_id: int) -> None:
    """Flag a video as not fully scanned.

    Called before wiping its faces for a rescan. Without this, cancelling a
    rescan (SIGTERM, no handler) leaves every not-yet-reached clip flagged
    complete with zero faces, is_scanned() skips it forever, and the people in
    it are gone with no way to get them back.
    """
    conn.execute("UPDATE videos SET scan_complete = 0 WHERE id = ?", (video_id,))
    conn.commit()


def clear_video_faces(conn: sqlite3.Connection, video_id: int) -> None:
    conn.execute("DELETE FROM faces WHERE video_id = ?", (video_id,))
    conn.commit()


def forget_missing_videos(conn: sqlite3.Connection, root: str) -> list[str]:
    """Drop index rows for clips under `root` whose file is no longer there.

    The index outlives the disk. Reorganising a folder after a scan leaves rows
    pointing at the old layout, and those ghosts are indistinguishable from real
    clips everywhere downstream: they inflate counts, they widen the tag
    vocabulary, and they are discarded only at the very last step, where the
    timeline checks whether the file is actually present. One tester's library
    carried 169 of them, so a 170-clip batch produced a one-clip timeline.

    Only ever called with a root that was just walked successfully, so an
    unplugged drive — where every path beneath it looks missing — cannot
    trigger a purge. Rows in `faces`, `frame_embeddings` and `auto_tags` follow
    the video out via ON DELETE CASCADE.

    Returns the paths forgotten.
    """
    prefix = root.rstrip("/") + "/"
    gone = [
        p for (p,) in conn.execute("SELECT path FROM videos").fetchall()
        if (p == root or p.startswith(prefix)) and not Path(p).exists()
    ]
    if gone:
        conn.executemany("DELETE FROM videos WHERE path = ?", [(p,) for p in gone])
        conn.commit()
    return gone


def add_frame_embeddings_bulk(
    conn: sqlite3.Connection,
    video_id: int,
    rows: list[tuple[float, bytes]],
) -> None:
    """Insert (timestamp, embedding-blob) rows for a video's sampled frames."""
    conn.executemany(
        "INSERT INTO frame_embeddings(video_id, timestamp_sec, embedding) VALUES (?, ?, ?)",
        [(video_id, t, blob) for t, blob in rows],
    )
    conn.commit()


def clear_video_embeddings(conn: sqlite3.Connection, video_id: int) -> None:
    conn.execute("DELETE FROM frame_embeddings WHERE video_id = ?", (video_id,))
    conn.commit()


def videos_with_embeddings(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Return [(video_id, path)] for videos that have at least one frame embedding."""
    rows = conn.execute(
        "SELECT DISTINCT v.id, v.path FROM videos v "
        "JOIN frame_embeddings fe ON fe.video_id = v.id "
        "ORDER BY v.path"
    ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def load_frame_embeddings(
    conn: sqlite3.Connection, video_id: int
) -> list[tuple[float, bytes]]:
    """Return [(timestamp, embedding-blob)] for a video."""
    rows = conn.execute(
        "SELECT timestamp_sec, embedding FROM frame_embeddings "
        "WHERE video_id = ? ORDER BY timestamp_sec",
        (video_id,),
    ).fetchall()
    return [(float(t), bytes(b)) for t, b in rows]


def replace_auto_tags(
    conn: sqlite3.Connection,
    video_id: int,
    tags: list[tuple[str, float]],
) -> None:
    """Wipe all auto-tags for a video and write the new set."""
    conn.execute("DELETE FROM auto_tags WHERE video_id = ?", (video_id,))
    if tags:
        conn.executemany(
            "INSERT INTO auto_tags(video_id, tag, score) VALUES (?, ?, ?)",
            [(video_id, t, s) for t, s in tags],
        )
    conn.commit()


def delete_auto_tags_by_name(conn: sqlite3.Connection, tags: set[str]) -> int:
    """Delete matched tags by name across ALL videos. Returns rows removed.

    Used to persist a review-screen rejection: when the user unchecks a tag
    before writing, we drop it from auto_tags so it also stops surfacing in the
    in-app search index and can't be re-written by a later tag-write. Matched
    tags are stored lowercased, so compare lowercased.
    """
    clean = {t.strip().lower() for t in tags if t and t.strip()}
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    cur = conn.execute(
        f"DELETE FROM auto_tags WHERE LOWER(tag) IN ({placeholders})",
        tuple(clean),
    )
    conn.commit()
    return cur.rowcount


def delete_auto_tag_pairs(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> int:
    """Delete specific (video path, tag) matches. Returns rows removed.

    The finer-grained sibling of `delete_auto_tags_by_name`. Unchecking a whole
    tag says "beach was never right"; unchecking one clip says "beach is right,
    just not in this one", which used to have no way to be said at all — the
    user's only option was dropping the tag from every clip it found.

    Persisted the same way and for the same reason: the rejection has to
    outlive this write, or the tag returns on the next one.
    """
    clean = [
        (p, t.strip().lower())
        for p, t in pairs
        if p and t and t.strip()
    ]
    if not clean:
        return 0
    removed = 0
    for path, tag in clean:
        cur = conn.execute(
            "DELETE FROM auto_tags WHERE LOWER(tag) = ? AND video_id IN "
            "(SELECT id FROM videos WHERE path = ?)",
            (tag, path),
        )
        removed += cur.rowcount
    conn.commit()
    return removed


def get_auto_tags(conn: sqlite3.Connection, video_id: int) -> list[tuple[str, float]]:
    """Return [(tag, score)] for a video, highest score first."""
    rows = conn.execute(
        "SELECT tag, score FROM auto_tags WHERE video_id = ? ORDER BY score DESC",
        (video_id,),
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def all_auto_tags(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return {video_path: [tag, ...]} for every video that has auto-tags."""
    rows = conn.execute(
        "SELECT v.path, a.tag FROM auto_tags a "
        "JOIN videos v ON v.id = a.video_id "
        "ORDER BY a.score DESC"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for path, tag in rows:
        out.setdefault(path, []).append(tag)
    return out


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
    """Every name already in the library, for the labeler's autocomplete.

    Filters blanks and keeps one spelling per name, so the suggestion list
    cannot itself offer the two variants that produce a split person.
    """
    rows = conn.execute(
        "SELECT name FROM people WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    seen: dict[str, str] = {}
    for (name,) in rows:
        key = name.strip().lower()
        if key and key not in seen:
            seen[key] = name.strip()
    return sorted(seen.values(), key=str.lower)


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

    Names are matched case- and whitespace-insensitively, so "Grayson",
    "grayson" and "Grayson " are one person rather than three. They used to
    group on the raw string, which meant a single inconsistent keystroke split
    someone permanently: the labeler would show two cards for them forever, and
    an editor searching one spelling would miss half their clips. The surviving
    row keeps the spelling of the largest cluster, on the same reasoning the
    canonical cluster is chosen — most of the faces were filed under it.

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

    # Keyed on the normalised name; the spellings are carried alongside so the
    # winner can be looked up once the largest cluster is known.
    by_name: dict[str, list[tuple[int, int, str]]] = {}
    for cid, name, cnt in rows:
        by_name.setdefault(name.strip().lower(), []).append(
            (int(cid), int(cnt), name)
        )

    merged: dict[str, list[int]] = {}
    for entries in by_name.values():
        entries.sort(key=lambda x: -x[1])  # largest cluster first
        canonical, _, winning_name = entries[0]
        if len(entries) <= 1:
            continue
        others = [cid for cid, _, _ in entries[1:]]
        for old_cid in others:
            conn.execute(
                "UPDATE faces SET cluster_id = ? WHERE cluster_id = ?",
                (canonical, old_cid),
            )
            conn.execute("DELETE FROM people WHERE cluster_id = ?", (old_cid,))
        # Settle on one spelling. Without this the merge would be invisible in
        # the keyword written into the files, which is the only place the user
        # actually sees the name.
        conn.execute(
            "UPDATE people SET name = ? WHERE cluster_id = ?",
            (winning_name, canonical),
        )
        merged[winning_name] = [cid for cid, _, _ in entries]

    conn.commit()
    return merged
