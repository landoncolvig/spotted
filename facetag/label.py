"""Interactive cluster labeling.

For each unlabeled cluster, generate a 3x3 grid of representative face crops,
open it in Preview, and prompt for a name in the terminal.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import cv2
import numpy as np

from . import db, extract


def _grab_frame(video_path: Path, t: float) -> np.ndarray | None:
    """Pull a single frame at time t (seconds) using ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", f"{t:.2f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    # We don't know dimensions back from rawvideo; use a probe-and-decode loop instead.
    return None


def _crop_face(video_path: Path, t: float, bbox: tuple[int, int, int, int]) -> np.ndarray | None:
    """Extract a single frame and return the face crop. Uses ffmpeg image2 + opencv decode."""
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
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    arr = np.frombuffer(proc.stdout, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None

    # bbox is in the *resized* coordinate space used during scanning (max_side=960).
    # Re-derive the scale by comparing to actual frame dims.
    H, W = img.shape[:2]
    src_max = 960  # must match extract.iter_frames max_side
    scale = min(1.0, src_max / max(W, H))
    scaled_w = int(round(W * scale))
    scaled_h = int(round(H * scale))
    x, y, w, h = bbox
    # Map back from scaled space to original
    inv = 1.0 / scale if scale > 0 else 1.0
    ox = int(x * inv)
    oy = int(y * inv)
    ow = int(w * inv)
    oh = int(h * inv)
    # Pad 30% for context.
    pad = int(0.3 * max(ow, oh))
    x0 = max(0, ox - pad)
    y0 = max(0, oy - pad)
    x1 = min(W, ox + ow + pad)
    y1 = min(H, oy + oh + pad)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (256, 256))


def _make_grid(crops: list[np.ndarray]) -> np.ndarray:
    cells = 9
    while len(crops) < cells:
        crops.append(np.zeros((256, 256, 3), dtype=np.uint8))
    crops = crops[:cells]
    rows = []
    for r in range(3):
        rows.append(np.hstack(crops[r * 3 : (r + 1) * 3]))
    return np.vstack(rows)


def _open_image_mac(path: Path) -> None:
    subprocess.run(["open", str(path)], check=False)


def label_clusters(conn, work_dir: Path, only_unnamed: bool = True) -> int:
    """Iterate clusters, prompt for a name. Returns count of clusters newly named."""
    summary = db.cluster_summary(conn)
    work_dir.mkdir(parents=True, exist_ok=True)
    named = 0

    print(f"\nFound {len(summary)} clusters. Press Enter (no name) to skip a cluster, type 'q' to quit.\n")

    for cluster_id, count, existing_name in summary:
        if only_unnamed and existing_name:
            continue
        samples = db.representative_faces(conn, cluster_id, n=9)
        crops: list[np.ndarray] = []
        for video_path, t, bbox in samples:
            c = _crop_face(Path(video_path), t, bbox)
            if c is not None:
                crops.append(c)
        if not crops:
            print(f"[cluster {cluster_id}] {count} faces — could not generate previews, skipping")
            continue
        grid = _make_grid(crops)
        out = work_dir / f"cluster_{cluster_id:04d}.jpg"
        cv2.imwrite(str(out), grid)
        _open_image_mac(out)

        prompt = f"[cluster {cluster_id}] {count} faces"
        if existing_name:
            prompt += f" (currently: {existing_name})"
        prompt += " — name? "
        try:
            name = input(prompt).strip()
        except EOFError:
            break
        if name.lower() == "q":
            break
        if not name:
            continue
        db.name_cluster(conn, cluster_id, name)
        named += 1
        print(f"  ✓ saved as {name!r}")
    return named
