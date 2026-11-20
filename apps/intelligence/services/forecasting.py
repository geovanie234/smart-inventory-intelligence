"""
Forecasting pipeline.

For every active product:
1. Build the daily demand series (zeros included, stockout days censored).
2. Evaluate every candidate model on a time-based holdout (last N days).
3. Select the model with the lowest WAPE (ties → simpler model).
4. Refit the selected model on the full history and forecast the horizon.
5. Store predictions with an 80% interval derived from holdout errors.

Portfolio-level results (all products combined) are stored for each model so the
team can see whether advanced models actually beat the moving-average baseline.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import Product
from apps.purchasing.services import on_order_map

from ..ml.evaluation import bias, holdout_evaluate, mae, wape
from ..ml.forecasters import HAS_SKLEARN, candidate_models
from ..models import DemandPrediction, ForecastRun, ModelEvaluation
from .data import build_demand_frames, series_for

logger = logging.getLogger(__name__)
Z_80 = 1.2816
SELECTED = "Selected per product"


def _q(x: float) -> Decimal:
    return Decimal(str(round(max(float(x), 0.0), 3)))


def run_forecast(business, *, horizon: int | None = None, holdout: int | None = None, user=None) -> tuple[ForecastRun, dict]:
    cfg = settings.INTELLIGENCE
    horizon = horizon or cfg["FORECAST_HORIZON_DAYS"]
    holdout = holdout or cfg["HOLDOUT_DAYS"]
    min_history = max(cfg["MIN_HISTORY_DAYS"], holdout + 14)
    today = timezone.localdate()
    end = today - timedelta(days=1)  # last complete day
    start = end - timedelta(days=cfg["HISTORY_WINDOW_DAYS"] - 1)

    run = ForecastRun.objects.create(
        business=business, horizon_days=horizon, holdout_days=holdout, triggered_by=user,
        history_start=start, history_end=end,
    )
    stats: dict[int, tuple[float, float]] = {}
    try:
        products = list(Product.objects.filter(business=business, is_active=True).select_related("business", "preferred_supplier"))
        on_order = on_order_map(business)
        sales, available = build_demand_frames(business, start, end, [p.pk for p in products])
        future = pd.date_range(today, periods=horizon, freq="D")
        pooled_actual, pooled_fc = defaultdict(list), defaultdict(list)
        baseline_flags = {}
        predictions, evaluations = [], []
        selected_counts = defaultdict(int)
        insufficient = 0

        for p in products:
            y, av = series_for(p.pk, sales, available)
            n_obs = int(av.sum())
            if n_obs >= min_history:
                results = []
                for model in candidate_models():
                    try:
                        results.append((model, holdout_evaluate(model, y, av, holdout)))
                    except Exception:  # a failing candidate must not stop the run
                        logger.exception("Model %s failed for product %s", model.name, p.pk)
                usable = [(m, r) for m, r in results if r.n_days > 0]
                if not usable:
                    n_obs = 0
                else:
                    best_model, best = min(usable, key=lambda mr: mr[1].sort_key)
                    for m, r in usable:
                        evaluations.append(ModelEvaluation(
                            run=run, product=p, model_name=r.model_name, mae=r.mae, wape=r.wape, bias=r.bias,
                            n_days=r.n_days, is_baseline=r.is_baseline, is_selected=m is best_model,
                        ))
                        pooled_actual[r.model_name].append(r.actual)
                        pooled_fc[r.model_name].append(r.forecast)
                        baseline_flags[r.model_name] = r.is_baseline
                    pooled_actual[SELECTED].append(best.actual)
                    pooled_fc[SELECTED].append(best.forecast)
                    best_model.fit(y, av)
                    preds = best_model.predict(future)
                    sigma = best.residual_std
                    model_name = best.model_name
                    selected_counts[model_name] += 1
            if n_obs < min_history:
                insufficient += 1
                obs = y[av.astype(bool)] if len(y) else pd.Series(dtype=float)
                if len(obs) == 0:
                    if p.forecast_daily_demand is not None:
                        p.forecast_daily_demand = None
                        p.stock_risk = p.compute_risk(on_order.get(p.pk, 0))
                        p.save(update_fields=["forecast_daily_demand", "stock_risk"])
                    continue
                level = float(obs.iloc[-28:].mean())
                preds = np.full(horizon, level)
                sigma = float(obs.iloc[-28:].std(ddof=0)) if len(obs) > 1 else level
                model_name = "Moving average (limited history)"

            for d, value in zip(future, preds):
                predictions.append(DemandPrediction(
                    business=business, run=run, product=p, forecast_date=d.date(), model_name=model_name,
                    predicted_quantity=_q(value), lower_bound=_q(value - Z_80 * sigma), upper_bound=_q(value + Z_80 * sigma),
                ))
            avg = float(np.mean(preds))
            stats[p.pk] = (avg, sigma)
            p.forecast_daily_demand = _q(avg)
            p.stock_risk = p.compute_risk(on_order.get(p.pk, 0))
            p.intelligence_updated_at = timezone.now()
            p.save(update_fields=["forecast_daily_demand", "stock_risk", "intelligence_updated_at"])

        with transaction.atomic():
            DemandPrediction.objects.bulk_create(predictions, batch_size=1000)
            ModelEvaluation.objects.bulk_create(evaluations, batch_size=1000)
            portfolio = []
            for name in pooled_actual:
                a, f = np.concatenate(pooled_actual[name]), np.concatenate(pooled_fc[name])
                portfolio.append(ModelEvaluation(
                    run=run, product=None, model_name=name, mae=mae(a, f), wape=wape(a, f), bias=bias(a, f),
                    n_days=len(a), is_baseline=baseline_flags.get(name, False), is_selected=name == SELECTED,
                ))
            ModelEvaluation.objects.bulk_create(portfolio)

        baseline = next((e for e in portfolio if e.is_baseline), None)
        chosen = next((e for e in portfolio if e.model_name == SELECTED), None)
        run.summary = {
            "products": len(products),
            "selected_models": dict(selected_counts),
            "baseline_wape": baseline.wape if baseline else None,
            "selected_wape": chosen.wape if chosen else None,
            "improvement_pct": (
                (baseline.wape - chosen.wape) / baseline.wape * 100
                if baseline and chosen and baseline.wape and chosen.wape is not None else None
            ),
            "sklearn": HAS_SKLEARN,
            "synthetic_data": business.is_demo,
        }
        run.products_forecast = len(products) - insufficient
        run.products_insufficient = insufficient
        run.status = ForecastRun.Status.SUCCESS
    except Exception as exc:
        logger.exception("Forecast run %s failed", run.pk)
        run.status = ForecastRun.Status.FAILED
        run.error = str(exc)[:2000]
        raise
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run, stats
