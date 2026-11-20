from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.intelligence.models import Alert, DemandPrediction, ReorderRecommendation
from apps.intelligence.services.evaluation import latest_run
from apps.inventory.models import Product
from apps.purchasing.models import PurchaseOrder
from apps.sales.models import Sale, SaleItem


def _revenue(business, start, end):
    return Sale.objects.filter(
        business=business, status=Sale.Status.COMPLETED, sold_at__date__gte=start, sold_at__date__lte=end
    ).aggregate(t=Sum("total_amount"), n=Count("id"))


def kpis(business) -> dict:
    today = timezone.localdate()
    cur = _revenue(business, today - timedelta(days=29), today)
    prev = _revenue(business, today - timedelta(days=59), today - timedelta(days=30))
    cur_t, prev_t = cur["t"] or Decimal("0"), prev["t"] or Decimal("0")
    products = Product.objects.filter(business=business, is_active=True)
    value = products.aggregate(
        v=Sum(ExpressionWrapper(F("stock_on_hand") * F("cost_price"), output_field=DecimalField(max_digits=20, decimal_places=2)))
    )["v"] or Decimal("0")
    risk = dict(products.values_list("stock_risk").annotate(n=Count("id")))
    return {
        "revenue_30d": float(cur_t),
        "revenue_change_pct": float((cur_t - prev_t) / prev_t * 100) if prev_t else None,
        "sales_30d": cur["n"],
        "stock_value": float(value),
        "active_products": products.count(),
        "at_risk": risk.get("out", 0) + risk.get("critical", 0) + risk.get("low", 0),
        "out_of_stock": risk.get("out", 0),
        "overstock": risk.get("overstock", 0),
        "risk_counts": risk,
        "open_alerts": Alert.objects.filter(business=business, status=Alert.Status.OPEN).count(),
        "critical_alerts": Alert.objects.filter(business=business, status=Alert.Status.OPEN, severity=Alert.Severity.CRITICAL).count(),
        "pending_recommendations": ReorderRecommendation.objects.filter(business=business, status=ReorderRecommendation.Status.PENDING).count(),
        "awaiting_approval": PurchaseOrder.objects.filter(business=business, status=PurchaseOrder.Status.SUBMITTED).count(),
        "open_orders": PurchaseOrder.objects.filter(business=business, status__in=PurchaseOrder.OPEN_STATUSES).count(),
    }


def sales_and_forecast_series(business, history_days=60) -> dict:
    """Daily units sold (all products) for the recent past, and the latest run's total forecast ahead."""
    today = timezone.localdate()
    start = today - timedelta(days=history_days)  # history ends yesterday: today is still incomplete
    daily_units = dict(
        SaleItem.objects.filter(sale__business=business, sale__status=Sale.Status.COMPLETED,
                                sale__sold_at__date__gte=start, sale__sold_at__date__lt=today)
        .annotate(d=TruncDate("sale__sold_at")).values_list("d").annotate(q=Sum("quantity"))
    )
    daily_rev = dict(
        Sale.objects.filter(business=business, status=Sale.Status.COMPLETED, sold_at__date__gte=start, sold_at__date__lt=today)
        .annotate(d=TruncDate("sold_at")).values_list("d").annotate(r=Sum("total_amount"))
    )
    history = [
        {"x": (start + timedelta(days=i)).isoformat(),
         "units": float(daily_units.get(start + timedelta(days=i), 0)),
         "revenue": float(daily_rev.get(start + timedelta(days=i), 0))}
        for i in range(history_days)
    ]
    run = latest_run(business)
    forecast = []
    if run:
        rows = (
            DemandPrediction.objects.filter(run=run, forecast_date__gte=today)
            .values("forecast_date").annotate(q=Sum("predicted_quantity"), lo=Sum("lower_bound"), hi=Sum("upper_bound"))
            .order_by("forecast_date")
        )
        forecast = [{"x": r["forecast_date"].isoformat(), "y": float(r["q"]), "lo": float(r["lo"]), "hi": float(r["hi"])} for r in rows]
    return {"history": history, "forecast": forecast}


def top_products(business, days=30, limit=8):
    since = timezone.localdate() - timedelta(days=days - 1)
    return list(
        SaleItem.objects.filter(sale__business=business, sale__status=Sale.Status.COMPLETED, sale__sold_at__date__gte=since)
        .values("product__id", "product__name").annotate(revenue=Sum("line_total"), qty=Sum("quantity"))
        .order_by("-revenue")[:limit]
    )
