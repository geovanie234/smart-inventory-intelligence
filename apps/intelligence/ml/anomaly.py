"""Anomaly detection primitives: robust statistical thresholds and Isolation Forest."""
from __future__ import annotations

import numpy as np

try:
    from sklearn.ensemble import IsolationForest

    HAS_SKLEARN = True
except ImportError:  # pragma: no cover
    HAS_SKLEARN = False


def robust_zscore(value: float, history) -> float:
    """
    Modified z-score (Iglewicz & Hoaglin) using median and MAD, resistant to the
    outliers we are trying to detect. |z| > 3.5 is a conventional anomaly threshold.
    """
    history = np.asarray(history, float)
    if len(history) < 5:
        return 0.0
    median = np.median(history)
    mad = np.median(np.abs(history - median))
    if mad == 0:
        mean_abs = np.mean(np.abs(history - median))
        if mean_abs == 0:
            return 0.0 if value == median else 10.0
        return float((value - median) / (1.253314 * mean_abs))
    return float(0.6745 * (value - median) / mad)


def isolation_forest_scores(X: np.ndarray, contamination: float = 0.05, random_state: int = 42):
    """
    Returns (is_outlier: bool[], score: float[]) where higher score = more anomalous.
    Falls back to per-feature robust z-scores when scikit-learn is unavailable.
    """
    X = np.asarray(X, float)
    if len(X) < 20:
        return np.zeros(len(X), dtype=bool), np.zeros(len(X))
    if HAS_SKLEARN:
        model = IsolationForest(n_estimators=200, contamination=contamination, random_state=random_state)
        labels = model.fit_predict(X)
        scores = -model.score_samples(X)
        return labels == -1, scores
    med = np.median(X, axis=0)
    mad = np.median(np.abs(X - med), axis=0)
    mad[mad == 0] = 1.0
    z = np.max(np.abs(0.6745 * (X - med) / mad), axis=1)
    return z > 3.5, z
