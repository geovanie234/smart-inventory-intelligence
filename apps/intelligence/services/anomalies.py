"""
Detection of unusual stock movements for human review (proposal §6).

Three layers:
1. `screen_transaction` — immediate statistical check on every manual stock-out
   (adjustment, damage, loss, expiry write-off) using a robust z-score against the
   product's own history and a comparison with typical daily sales.
2. `screen_stock_count` — large shortages or surpluses found at physical counts.
3. `detect_anomalies` — periodic Isolation Forest over recent movements, plus
   detection of unusual daily sales spikes (possible data-entry errors or demand shocks).

Anomalies are *flags for review*, never automatic corrections.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from django.db.models import Sum
from django.utils import timezone

from apps.inventory.models import InventoryTransaction, Product, StockCount
from apps.common.utils import fmt_qty
from apps.sales.models import Sale, SaleItem

from ..ml.anomaly import isolation_forest_scores, robust_zscore
from ..models import Alert
from .alerts import raise_alert

T = InventoryTransaction.Type
SCREENED_OUTFLOWS = [T.ADJUSTMENT_OUT, T.DAMAGE, T.LOSS, T.EXPIRED]
Z_THRESHOLD = 3.5


def _typical_daily_sales(product, days=30) -> float:
    if product.forecast_daily_demand:
        return float(product.forecast_daily_demand)
    since = timezone.now() - timedelta(days=days)
    total = SaleItem.objects.filter(
        product=product, sale__status=Sale.Status.COMPLETED, sale__sold_at__gte=since
    ).aggregate(q=Sum("quantity"))["q"]
    return float(total or 0) / days


def screen_transaction(txn: InventoryTransaction) -> Alert | None:
    if txn.txn_type not in SCREENED_OUTFLOWS or txn.quantity >= 0:
        return None
    qty = float(abs(txn.quantity))
    product = txn.product
    history = list(
        InventoryTransaction.objects.filter(
            product=product, txn_type__in=SCREENED_OUTFLOWS,
            occurred_at__gte=txn.occurred_at - timedelta(days=180),
        ).exclude(pk=txn.pk).values_list("quantity", flat=True)
    )
    history = [abs(float(h)) for h in history]
    z = robust_zscore(qty, history) if len(history) >= 5 else 0.0
    daily = _typical_daily_sales(product)
    days_equiv = qty / daily if daily > 0 else None
    stock_before = float(txn.balance_after) + qty
    share = qty / stock_before if stock_before > 0 else 1.0

    reasons = []
    if z > Z_THRESHOLD:
        reasons.append(f"{z:.1f}× more unusual than this product's past write-offs (robust z-score)")
    if days_equiv is not None and days_equiv >= 3:
        reasons.append(f"equal to about {days_equiv:.1f} days of normal sales")
    if share >= 0.25 and qty >= 5:
        reasons.append(f"{share:.0%} of the stock that was on hand")
    if not reasons:
        return None
    severity = Alert.Severity.CRITICAL if (z > 6 or (days_equiv or 0) >= 7) and txn.txn_type == T.LOSS else Alert.Severity.WARNING
    unit = product.unit
    alert, _ = raise_alert(
        txn.business, Alert.Type.ANOMALY, f"anomaly:txn:{txn.pk}",
        f"Unusual {txn.get_txn_type_display().lower()} of {product.name}",
        f"{qty:g} {unit} recorded as {txn.get_txn_type_display().lower()} on "
        f"{timezone.localtime(txn.occurred_at):%d %b %Y %H:%M}"
        f"{' by ' + str(txn.created_by) if txn.created_by else ''}: " + "; ".join(reasons) + ". Please verify.",
        severity, product=product, transaction=txn, score=round(max(z, days_equiv or 0), 2),
    )
    return alert


def screen_stock_count(count: StockCount) -> Alert | None:
    if count.variance == 0:
        return None
    variance = float(count.variance)
    system = float(count.system_quantity) or 1.0
    pct = abs(variance) / system
    daily = _typical_daily_sales(count.product)
    if pct < 0.10 and (daily <= 0 or abs(variance) < 2 * daily):
        return None
    shortage = variance < 0
    p = count.product
    alert, _ = raise_alert(
        count.business, Alert.Type.ANOMALY, f"anomaly:count:{count.pk}",
        f"Stock count {'shortage' if shortage else 'surplus'}: {p.name}",
        f"Physical count {fmt_qty(count.counted_quantity)} vs system {fmt_qty(count.system_quantity)} "
        f"({variance:+g}, {pct:.0%}). "
        + ("Possible unrecorded sales, theft or damage." if shortage else "Possible unrecorded receipt or mis-scan.")
        + " The ledger has been corrected; please investigate the cause.",
        Alert.Severity.WARNING if shortage else Alert.Severity.INFO, product=p,
        transaction=count.transaction, score=round(pct * 100, 1),
    )
    return alert


def detect_anomalies(business, lookback_days: int = 120, recent_days: int = 7) -> int:
    now = timezone.now()
    raised = 0
    # --- Isolation Forest over manual stock-outs ------------------------------
    txns = list(
        InventoryTransaction.objects.filter(
            business=business, txn_type__in=SCREENED_OUTFLOWS + [T.COUNT_CORRECTION],
            occurred_at__gte=now - timedelta(days=lookback_days), quantity__lt=0,
        ).select_related("product", "created_by")
    )
    if len(txns) >= 20:
        type_codes = {t: i for i, t in enumerate(SCREENED_OUTFLOWS + [T.COUNT_CORRECTION])}
        daily_cache = {}
        rows = []
        for t in txns:
            if t.product_id not in daily_cache:
                daily_cache[t.product_id] = _typical_daily_sales(t.product) or 1.0
            local = timezone.localtime(t.occurred_at)
            q = abs(float(t.quantity))
            onehot = [0.0] * len(type_codes)
            onehot[type_codes[t.txn_type]] = 1.0
            rows.append([np.log1p(q), q / daily_cache[t.product_id], local.hour / 23, local.weekday() / 6, *onehot])
        outliers, scores = isolation_forest_scores(np.array(rows), contamination=0.05)
        cutoff = now - timedelta(days=recent_days)
        for t, flag, score in zip(txns, outliers, scores):
            if not flag or t.occurred_at < cutoff:
                continue
            q = abs(float(t.quantity))
            _, created = raise_alert(
                business, Alert.Type.ANOMALY, f"anomaly:txn:{t.pk}",
                f"Unusual stock movement: {t.product.name}",
                f"{t.get_txn_type_display()} of {q:g} {t.product.unit} on "
                f"{timezone.localtime(t.occurred_at):%d %b %Y %H:%M} stands out from recent movement patterns "
                f"(Isolation Forest score {score:.2f}). Please verify the record.",
                Alert.Severity.WARNING, product=t.product, transaction=t, score=round(float(score), 3),
            )
            raised += created

    # --- Daily sales spikes -----------------------------------------------------
    since = timezone.localdate() - timedelta(days=60)
    rows = (
        SaleItem.objects.filter(sale__business=business, sale__status=Sale.Status.COMPLETED, sale__sold_at__date__gte=since)
        .values("product_id", "product__name", "sale__sold_at__date").annotate(q=Sum("quantity"))
    )
    by_product: dict[int, dict] = {}
    names = {}
    for r in rows:
        by_product.setdefault(r["product_id"], {})[r["sale__sold_at__date"]] = float(r["q"])
        names[r["product_id"]] = r["product__name"]
    recent_start = timezone.localdate() - timedelta(days=recent_days)
    products = Product.objects.in_bulk(list(by_product))
    for pid, days in by_product.items():
        all_days = [since + timedelta(days=i) for i in range(61)]
        history = [days.get(d, 0.0) for d in all_days if d < recent_start]
        for d in all_days:
            if d < recent_start or d not in days:
                continue
            z = robust_zscore(days[d], history)
            if z > 6 and days[d] >= 5 and days[d] > 3 * (np.mean(history) if history else 0):
                _, created = raise_alert(
                    business, Alert.Type.ANOMALY, f"anomaly:spike:{pid}:{d}",
                    f"Unusual sales volume: {names[pid]}",
                    f"{days[d]:g} units sold on {d:%d %b %Y} versus a typical {np.median(history):g}/day. "
                    "Check for a data-entry error, a bulk customer order or a demand shock.",
                    Alert.Severity.INFO, product=products.get(pid), score=round(z, 2),
                )
                raised += created
    return raised
