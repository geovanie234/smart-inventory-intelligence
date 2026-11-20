"""
Candidate demand-forecasting models.

All models share one interface:

    model.fit(y: pd.Series, available: pd.Series[bool])
    model.predict(dates: pd.DatetimeIndex) -> np.ndarray

`y` is daily unit sales indexed by date (zeros included). `available` marks days
on which the product was actually in stock: days that ended out of stock are
treated as *censored* (sales understate demand) and are excluded from fitting.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .calendar import calendar_features

try:  # scikit-learn is required for the ML candidates; baselines work without it
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.linear_model import Ridge

    HAS_SKLEARN = True
except ImportError:  # pragma: no cover
    HAS_SKLEARN = False

try:  # optional
    from xgboost import XGBRegressor

    HAS_XGBOOST = True
except ImportError:  # pragma: no cover
    HAS_XGBOOST = False


class Forecaster:
    name = "base"
    is_baseline = False
    complexity = 0  # tie-breaker: prefer simpler models

    def fit(self, y: pd.Series, available: pd.Series) -> "Forecaster":
        raise NotImplementedError

    def predict(self, dates: pd.DatetimeIndex) -> np.ndarray:
        raise NotImplementedError


class MovingAverage(Forecaster):
    """Mean of the last `window` in-stock days. The reference baseline."""

    complexity = 0

    def __init__(self, window: int = 28):
        self.window = window
        self.name = f"Moving average ({window}d)"
        self.is_baseline = window == 28

    def fit(self, y, available):
        obs = y[available.astype(bool)]
        self.level_ = float(obs.iloc[-self.window:].mean()) if len(obs) else 0.0
        return self

    def predict(self, dates):
        return np.full(len(dates), self.level_)


class SeasonalNaive(Forecaster):
    """Average of the same weekday over the last `weeks` weeks (captures weekly patterns)."""

    name = "Seasonal weekday average"
    complexity = 1

    def __init__(self, weeks: int = 6):
        self.weeks = weeks

    def fit(self, y, available):
        obs = y[available.astype(bool)].iloc[-7 * self.weeks:]
        overall = float(obs.mean()) if len(obs) else 0.0
        by_dow = obs.groupby(obs.index.dayofweek).mean()
        self.profile_ = {d: float(by_dow.get(d, overall)) for d in range(7)}
        return self

    def predict(self, dates):
        return np.array([self.profile_[d] for d in dates.dayofweek])


class _FeatureModel(Forecaster):
    """Regression on calendar features plus a recent-demand level known at the forecast origin."""

    level_window = 28

    def _make_estimator(self):
        raise NotImplementedError

    def _level_series(self, y, available):
        masked = y.where(available.astype(bool))
        # level known at the start of each day: mean of previous `level_window` in-stock days
        return masked.rolling(self.level_window, min_periods=7).mean().shift(1)

    def fit(self, y, available):
        self.origin_ = y.index[0]
        level = self._level_series(y, available)
        X = calendar_features(y.index, self.origin_)
        X["level"] = level
        mask = available.astype(bool) & level.notna()
        self.fallback_ = float(y[available.astype(bool)].mean()) if available.any() else 0.0
        self.last_level_ = float(y.where(available.astype(bool)).iloc[-self.level_window:].mean())
        if np.isnan(self.last_level_):
            self.last_level_ = self.fallback_
        if mask.sum() < 21:
            self.estimator_ = None
            return self
        self.columns_ = list(X.columns)
        self.estimator_ = self._make_estimator()
        self.estimator_.fit(X[mask].values, y[mask].values)
        return self

    def predict(self, dates):
        if self.estimator_ is None:
            return np.full(len(dates), self.fallback_)
        X = calendar_features(dates, self.origin_)
        X["level"] = self.last_level_
        return np.clip(self.estimator_.predict(X[self.columns_].values), 0, None)


class LinearRegressionModel(_FeatureModel):
    name = "Linear regression"
    complexity = 2

    def _make_estimator(self):
        if HAS_SKLEARN:
            return Ridge(alpha=1.0)
        return _NumpyLeastSquares()


class RandomForestModel(_FeatureModel):
    name = "Random forest"
    complexity = 3

    def _make_estimator(self):
        return RandomForestRegressor(
            n_estimators=150, max_depth=8, min_samples_leaf=3, random_state=42, n_jobs=1
        )


class GradientBoostingModel(_FeatureModel):
    name = "XGBoost" if HAS_XGBOOST else "Gradient boosting"
    complexity = 4

    def _make_estimator(self):
        if HAS_XGBOOST:
            return XGBRegressor(
                n_estimators=200, max_depth=4, learning_rate=0.05, subsample=0.9,
                objective="reg:squarederror", random_state=42, n_jobs=1, verbosity=0,
            )
        return HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_depth=4, random_state=42)


class _NumpyLeastSquares:
    """Tiny ridge regression used only when scikit-learn is unavailable."""

    def fit(self, X, y, alpha=1.0):
        X1 = np.c_[np.ones(len(X)), X]
        reg = alpha * np.eye(X1.shape[1])
        reg[0, 0] = 0
        self.coef_ = np.linalg.solve(X1.T @ X1 + reg, X1.T @ y)
        return self

    def predict(self, X):
        return np.c_[np.ones(len(X)), X] @ self.coef_


def candidate_models() -> list[Forecaster]:
    models: list[Forecaster] = [MovingAverage(28), MovingAverage(7), SeasonalNaive(), LinearRegressionModel()]
    if HAS_SKLEARN:
        models += [RandomForestModel(), GradientBoostingModel()]
    return models
