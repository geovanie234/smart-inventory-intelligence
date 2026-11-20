"""End-to-end intelligence cycle: forecast → recommendations → alerts → anomaly screening."""
from __future__ import annotations

import logging

from .alerts import evaluate_expiry_alerts, evaluate_stock_alerts
from .anomalies import detect_anomalies
from .forecasting import run_forecast
from .recommendations import generate_recommendations

logger = logging.getLogger(__name__)


def run_intelligence(business, *, user=None, horizon=None, holdout=None) -> dict:
    run, stats = run_forecast(business, horizon=horizon, holdout=holdout, user=user)
    recs = generate_recommendations(business, run, stats)
    stock_alerts = evaluate_stock_alerts(business)
    expiry_alerts = evaluate_expiry_alerts(business)
    anomalies = detect_anomalies(business)
    try:
        from apps.iot.services import check_device_health

        device_alerts = check_device_health(business)
    except Exception:  # IoT is optional; never block the core cycle
        logger.exception("IoT health check failed")
        device_alerts = 0
    result = {
        "run": run,
        "recommendations": len(recs),
        "new_stock_alerts": stock_alerts,
        "new_expiry_alerts": expiry_alerts,
        "new_anomalies": anomalies,
        "new_device_alerts": device_alerts,
    }
    run.summary.update({k: v for k, v in result.items() if k != "run"})
    run.save(update_fields=["summary"])
    return result
