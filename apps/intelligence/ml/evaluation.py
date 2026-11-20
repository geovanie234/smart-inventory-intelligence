"""Forecast error metrics and leakage-free time-based holdout evaluation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # avoid importing scikit-learn just to compute metrics
    from .forecasters import Forecaster


def mae(actual, forecast) -> float:
    actual, forecast = np.asarray(actual, float), np.asarray(forecast, float)
    return float(np.mean(np.abs(actual - forecast))) if len(actual) else float("nan")


def wape(actual, forecast) -> float | None:
    """Weighted absolute percentage error = Σ|A−F| / Σ|A| × 100. Undefined when there were no sales."""
    actual, forecast = np.asarray(actual, float), np.asarray(forecast, float)
    denom = np.sum(np.abs(actual))
    if denom == 0:
        return None
    return float(np.sum(np.abs(actual - forecast)) / denom * 100)


def bias(actual, forecast) -> float:
    actual, forecast = np.asarray(actual, float), np.asarray(forecast, float)
    return float(np.mean(forecast - actual)) if len(actual) else 0.0


@dataclass
class HoldoutResult:
    model_name: str
    mae: float
    wape: float | None
    bias: float
    n_days: int
    is_baseline: bool
    complexity: int
    actual: np.ndarray
    forecast: np.ndarray
    residual_std: float

    @property
    def sort_key(self):
        # Rank by WAPE (fall back to MAE), then prefer the simpler model
        primary = self.wape if self.wape is not None else self.mae * 100
        return (round(primary, 6), self.complexity)


def holdout_evaluate(model: "Forecaster", y: pd.Series, available: pd.Series, holdout_days: int) -> HoldoutResult:
    """
    Train on everything before the final `holdout_days`, forecast that period in one
    shot from the training cut-off (no future data leaks into training) and score
    only on in-stock days, where recorded sales reflect true demand.
    """
    train_y, test_y = y.iloc[:-holdout_days], y.iloc[-holdout_days:]
    train_av, test_av = available.iloc[:-holdout_days], available.iloc[-holdout_days:]
    model.fit(train_y, train_av)
    pred = model.predict(test_y.index)
    mask = test_av.astype(bool).values
    actual, fc = test_y.values[mask], pred[mask]
    return HoldoutResult(
        model_name=model.name,
        mae=mae(actual, fc),
        wape=wape(actual, fc),
        bias=bias(actual, fc),
        n_days=int(mask.sum()),
        is_baseline=model.is_baseline,
        complexity=model.complexity,
        actual=actual,
        forecast=fc,
        residual_std=float(np.std(actual - fc)) if len(actual) > 1 else 0.0,
    )
