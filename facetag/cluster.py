"""Cluster ArcFace embeddings into per-person groups.

ArcFace embeddings are L2-normalized, so squared Euclidean distance and cosine
distance carry the same ordering: ||a-b||^2 = 2 - 2*cos(a,b). We use euclidean
because HDBSCAN's fast paths support it.
"""
from __future__ import annotations

import numpy as np


def cluster_embeddings(
    embeddings: np.ndarray,
    min_cluster_size: int = 5,
    epsilon: float = 0.55,
) -> np.ndarray:
    """Return a cluster label for each row in `embeddings`.

    Noise points get label -1. Labels are 0-indexed dense ints otherwise.
    `epsilon` is in normalized-euclidean space; ~0.5-0.6 is a good starting point
    for ArcFace 512-d embeddings (matches a cosine similarity of ~0.85).
    """
    import hdbscan

    if len(embeddings) == 0:
        return np.array([], dtype=np.int64)

    if len(embeddings) < min_cluster_size:
        # Too few points for HDBSCAN — treat them as one cluster if any, else noise.
        return np.zeros(len(embeddings), dtype=np.int64)

    clusterer = hdbscan.HDBSCAN(
        metric="euclidean",
        min_cluster_size=min_cluster_size,
        min_samples=2,
        cluster_selection_epsilon=epsilon,
        cluster_selection_method="leaf",
    )
    labels = clusterer.fit_predict(embeddings.astype(np.float64))
    return labels.astype(np.int64)


def incremental_assign(
    new_embeddings: np.ndarray,
    existing_centroids: dict[int, np.ndarray],
    next_cluster_id: int,
    *,
    min_cluster_size: int = 5,
    epsilon: float = 0.68,
    match_threshold: float = 0.68,
) -> list[int]:
    """Assign `new_embeddings` to clusters without disturbing existing IDs.

    Two-stage:

    1. **Nearest-centroid match.** For each new face, find the closest
       existing cluster centroid. If euclidean distance < `match_threshold`,
       inherit that cluster_id — so a face from a new clip auto-joins
       "Sarah" if she's already in the index, keeping her saved label.

    2. **Form new clusters from the leftover.** Faces that didn't match
       any existing centroid go through HDBSCAN with the same params as
       the original full-library pass. New cluster IDs start at
       `next_cluster_id` so they never collide with existing ones.

    Returns a list of cluster_ids parallel to `new_embeddings`. Noise
    points get -1 (matching the convention of `cluster_embeddings`).
    """
    n = len(new_embeddings)
    if n == 0:
        return []
    out: list[int] = [-1] * n
    unmatched_idxs: list[int] = []

    # Stage 1: match to existing centroids
    if existing_centroids:
        cids = list(existing_centroids.keys())
        cents = np.stack([existing_centroids[c] for c in cids]).astype(np.float64)
        new = new_embeddings.astype(np.float64)
        # |a-b|^2 = |a|^2 + |b|^2 - 2ab, computed in row chunks.
        # The obvious (n, 1, d) - (1, k, d) broadcast materialises an
        # (n, k, d) float64 tensor first: 5,000 new faces against 200 existing
        # clusters is a 4.1 GB single allocation, and a large library raises
        # MemoryError after the whole scan has already completed.
        cent_sq = (cents ** 2).sum(axis=1)
        best = np.empty(n, dtype=np.int64)
        best_d = np.empty(n, dtype=np.float64)
        CHUNK = 2048
        for start in range(0, n, CHUNK):
            block = new[start : start + CHUNK]
            d2 = (
                (block ** 2).sum(axis=1)[:, None]
                + cent_sq[None, :]
                - 2.0 * (block @ cents.T)
            )
            np.maximum(d2, 0.0, out=d2)   # tiny negatives from float error
            bi = d2.argmin(axis=1)
            best[start : start + len(block)] = bi
            best_d[start : start + len(block)] = np.sqrt(
                d2[np.arange(len(block)), bi]
            )
        for i in range(n):
            if best_d[i] < match_threshold:
                out[i] = cids[best[i]]
            else:
                unmatched_idxs.append(i)
    else:
        unmatched_idxs = list(range(n))

    # Stage 2: HDBSCAN on the leftover
    if unmatched_idxs:
        leftover = new_embeddings[unmatched_idxs]
        local_labels = cluster_embeddings(
            leftover, min_cluster_size=min_cluster_size, epsilon=epsilon
        )
        # Remap dense [0..k) local labels to global IDs starting at next_cluster_id
        uniq = sorted({int(l) for l in local_labels if l >= 0})
        remap = {l: next_cluster_id + i for i, l in enumerate(uniq)}
        for local_i, lbl in zip(unmatched_idxs, local_labels):
            out[local_i] = remap[int(lbl)] if lbl >= 0 else -1

    return out
