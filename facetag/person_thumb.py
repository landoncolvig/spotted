"""Generate small face thumbnails per named person for the Library view.

The label-web grid (`facetag/label.py`) makes 3x3 face crops grouped by
cluster. For the library sidebar we want a single, tight face crop per
*person* (not per cluster) — what you'd see as the avatar next to each
name in the sidebar list.

Output: `~/.facetag/person_thumbs/<cluster_id>.jpg` (canonical cluster
id used since after auto-merge each named person maps to one cluster).
Size: 128x128 square crop, JPEG q85.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import db


PERSON_THUMB_DIR = Path.home() / ".facetag" / "person_thumbs"
THUMB_SIZE = 128


def _crop_one_face(video_path: Path, t: float, bbox: tuple[int, int, int, int]) -> np.ndarray | None:
    """Pull a single frame at timestamp `t` and crop to bbox (resized index space).

    Mirrors the logic in label._crop_face but returns a 128x128 square
    crop instead of the raw rectangle. Returns None on any failure.
    """
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{max(0, t):.2f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=15)
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    H, W = img.shape[:2]
    src_max = 960  # must match extract.iter_frames max_side
    scale = min(1.0, src_max / max(W, H))
    scaled_w = int(round(W * scale))
    scaled_h = int(round(H * scale))
    x, y, w, h = bbox
    # Map back from scaled space to original
    fx = W / scaled_w if scaled_w else 1.0
    fy = H / scaled_h if scaled_h else 1.0
    x = int(x * fx); y = int(y * fy)
    w = int(w * fx); h = int(h * fy)

    # Add 25% padding around the face so the thumbnail has some context
    pad_x = int(w * 0.25)
    pad_y = int(h * 0.25)
    x0 = max(0, x - pad_x); y0 = max(0, y - pad_y)
    x1 = min(W, x + w + pad_x); y1 = min(H, y + h + pad_y)
    if x1 <= x0 or y1 <= y0:
        return None

    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None

    # Square-pad and resize
    ch, cw = crop.shape[:2]
    side = max(ch, cw)
    sq = np.zeros((side, side, 3), dtype=np.uint8)
    yo = (side - ch) // 2
    xo = (side - cw) // 2
    sq[yo : yo + ch, xo : xo + cw] = crop
    return cv2.resize(sq, (THUMB_SIZE, THUMB_SIZE), interpolation=cv2.INTER_AREA)


def generate_person_thumbs(conn: sqlite3.Connection, *, force: bool = False) -> list[int]:
    """Write a thumbnail per named person to PERSON_THUMB_DIR.

    Returns the list of cluster_ids successfully written. Skips clusters
    whose thumbnails already exist on disk (unless `force=True`).
    """
    PERSON_THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # All named clusters, one row per cluster_id
    rows = conn.execute(
        "SELECT p.cluster_id "
        "FROM people p "
        "WHERE p.name IS NOT NULL AND p.name != ''"
    ).fetchall()

    written: list[int] = []
    for (cid,) in rows:
        out = PERSON_THUMB_DIR / f"{cid}.jpg"
        if out.exists() and not force:
            written.append(int(cid))
            continue
        samples = db.representative_faces(conn, int(cid), n=8)
        for video_path, t, bbox in samples:
            try:
                crop = _crop_one_face(Path(video_path), float(t), bbox)
            except Exception:
                continue
            if crop is None:
                continue
            cv2.imwrite(str(out), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            written.append(int(cid))
            break  # one good thumbnail is enough
    return written
