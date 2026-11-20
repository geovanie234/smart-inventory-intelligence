"""
System evaluation (proposal §13):

1. Forecast accuracy: holdout MAE/WAPE vs the baseline (stored per run) and
   *realised* accuracy of forecasts once actual sales are known.
2. Alert quality: precision from staff feedback; stockout-alert recall measured
   against stockouts that actually happened.
3. Stock record accuracy against physical counts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd
from django.db.models import Count, Q
from django.utils import timezone

from apps.inventory.models import InventoryTransaction, StockCount

from ..ml.evaluation import bias, mae, wape
from ..models import Alert, DemandPrediction, ForecastRun, ModelEvaluation
from .data import build_demand_frames


def latest_run(business):
    return ForecastRun.objects.filter(business=business, status=ForecastRun.Status.SUCCESS).order_by("-started_at").first()


def model_comparison(run) -> list[dict]:
    if run is None:
        return []
    rows = list(ModelEvaluation.objects.filter(run=run, product__isnull=True))
    base = next((r for r in rows if r.is_baseline), None)
    out = []
    for r in sorted(rows, key=lambda r: (r.wape if r.wape is not None else 1e9)):
        improvement = None
        if base and base.wape and r.wape is not None:
            improvement = (base.wape - r.wape) / base.wape * 100
        out.append({"model": r.model_name, "mae": r.mae, "wape": r.wape, "bias": r.bias, "n": r.n_days,
                    "is_baseline": r.is_baseline, "is_selected": r.is_selected, "improvement": improvement})
    return out


def realised_accuracy(business, days: int = 28) -> dict:
    """Compare stored ex-ante forecasts with what actually sold (in-stock days only)."""
    today = timezone.localdate()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    preds = (
        DemandPrediction.objects.filter(business=business, forecast_date__gte=start, forecast_date__lte=end)
        .values("product_id", "forecast_date", "run_id", "predicted_quantity", "run__started_at")
    )
    latest = {}
    for p in preds:
        if timezone.localtime(p["run__started_at"]).date() > p["forecast_date"]:
            continue  # only genuine ex-ante forecasts
        key = (p["product_id"], p["forecast_date"])
        if key not in latest or p["run_id"] > latest[key][0]:
            latest[key] = (p["run_id"], float(p["predicted_quantity"]))
    if not latest:
        return {"n": 0}
    product_ids = sorted({k[0] for k in latest})
    sales, available = build_demand_frames(business, start, end, product_ids)
    actual, fc = [], []
    for (pid, d), (_, value) in latest.items():
        ts = pd.Timestamp(d)
        if pid in available.columns and available.at[ts, pid]:
            actual.append(float(sales.at[ts, pid]))
            fc.append(value)
    if not actual:
        return {"n": 0}
    return {"n": len(actual), "mae": mae(actual, fc), "wape": wape(actual, fc), "bias": bias(actual, fc),
            "start": start, "end": end}


def alert_quality(business, days: int = 90) -> dict:
    since = timezone.now() - timedelta(days=days)
    rows = (
        Alert.objects.filter(business=business, created_at__gte=since)
        .values("alert_type")
        .annotate(
            total=Count("id"),
            useful=Count("id", filter=Q(feedback=Alert.Feedback.USEFUL)),
            false_alarm=Count("id", filter=Q(feedback=Alert.Feedback.FALSE_ALARM)),
        )
    )
    labels = dict(Alert.Type.choices)
    precision = []
    for r in rows:
        reviewed = r["useful"] + r["false_alarm"]
        precision.append({
            "type": labels.get(r["alert_type"], r["alert_type"]), "total": r["total"], "reviewed": reviewed,
            "precision": r["useful"] / reviewed * 100 if reviewed else None,
        })

    # Stockout recall: every time stock hit zero, had a risk alert been raised beforehand?
    events = InventoryTransaction.objects.filter(
        business=business, occurred_at__gte=since, quantity__lt=0, balance_after__lte=0
    ).values("product_id", "occurred_at", "quantity", "balance_after")
    stockouts = [e for e in events if e["balance_after"] - e["quantity"] > 0]  # transitioned to zero
    risk_alerts = defaultdict(list)
    for a in Alert.objects.filter(business=business, alert_type=Alert.Type.STOCKOUT_RISK,
                                  created_at__gte=since - timedelta(days=14)).values("product_id", "created_at"):
        risk_alerts[a["product_id"]].append(a["created_at"])
    detected, warning_days = 0, []
    for e in stockouts:
        prior = [t for t in risk_alerts[e["product_id"]] if e["occurred_at"] - timedelta(days=14) <= t < e["occurred_at"]]
        if prior:
            detected += 1
            warning_days.append((e["occurred_at"] - min(prior)).total_seconds() / 86400)
    return {
        "precision": sorted(precision, key=lambda r: -r["total"]),
        "stockouts": len(stockouts),
        "stockouts_detected": detected,
        "recall": detected / len(stockouts) * 100 if stockouts else None,
        "avg_warning_days": float(np.mean(warning_days)) if warning_days else None,
    }


def stock_record_accuracy(business, days: int = 90, tolerance_pct: float = 2.0) -> dict:
    counts = list(StockCount.objects.filter(business=business, counted_at__gte=timezone.now() - timedelta(days=days)))
    if not counts:
        return {"n": 0}
    accurate = [c for c in counts if abs(float(c.variance)) <= max(1.0, float(c.system_quantity) * tolerance_pct / 100)]
    abs_pct = [abs(float(c.variance)) / float(c.system_quantity) * 100 for c in counts if c.system_quantity]
    return {
        "n": len(counts),
        "accuracy": len(accurate) / len(counts) * 100,
        "mean_abs_variance_pct": float(np.mean(abs_pct)) if abs_pct else None,
        "tolerance_pct": tolerance_pct,
    }
