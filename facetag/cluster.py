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
