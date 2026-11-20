"""Alert lifecycle: de-duplicated raising, automatic resolution and rule-based stock/expiry alerts."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.inventory.models import Product, StockBatch
from apps.common.utils import fmt_qty

from ..models import Alert

A = Alert.Type
ACTIVE = (Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED)
STOCK_TYPES = (A.OUT_OF_STOCK, A.STOCKOUT_RISK, A.OVERSTOCK)


def raise_alert(business, alert_type, dedupe_key, title, message, severity=Alert.Severity.WARNING,
                product=None, **extra) -> tuple[Alert, bool]:
    existing = Alert.objects.filter(business=business, dedupe_key=dedupe_key, status__in=ACTIVE).first()
    if existing:
        changed = existing.severity != severity or existing.message != message
        existing.title, existing.message, existing.severity = title, message, severity
        for k, v in extra.items():
            setattr(existing, k, v)
        existing.save()
        return existing, False
    return Alert.objects.create(
        business=business, alert_type=alert_type, dedupe_key=dedupe_key, title=title, message=message,
        severity=severity, product=product, **extra,
    ), True


def auto_resolve(business, alert_types, keep_keys, note="Condition cleared automatically.") -> int:
    return (
        Alert.objects.filter(business=business, alert_type__in=alert_types, status__in=ACTIVE)
        .exclude(dedupe_key__in=keep_keys)
        .update(status=Alert.Status.RESOLVED, resolved_at=timezone.now(), resolution_note=note)
    )


def _position_text(p: Product) -> str:
    on_order = p.on_order()
    demand = float(p.forecast_daily_demand or 0)
    position = float(p.stock_on_hand + on_order)
    unit = p.unit
    text = f"{fmt_qty(p.stock_on_hand)} {unit} on hand"
    if on_order:
        text += f" + {fmt_qty(on_order)} on order"
    if demand:
        text += f" covers about {position / demand:.1f} days of forecast demand ({demand:.1f}/day)"
    return text


def _stock_alert_spec(p: Product):
    cover = p.days_of_cover
    lead = p.effective_lead_time
    if p.stock_risk == Product.Risk.OUT:
        return (A.OUT_OF_STOCK, f"out:{p.pk}", Alert.Severity.CRITICAL, f"{p.name} is out of stock",
                f"No stock on hand. Customers asking for {p.name} cannot be served until it is replenished.")
    if p.stock_risk == Product.Risk.CRITICAL:
        return (A.STOCKOUT_RISK, f"risk:{p.pk}", Alert.Severity.CRITICAL, f"{p.name} will run out before a delivery can arrive",
                f"{_position_text(p)}, less than the {lead}-day supplier lead time. Order now and consider an urgent delivery.")
    if p.stock_risk == Product.Risk.LOW:
        return (A.STOCKOUT_RISK, f"risk:{p.pk}", Alert.Severity.WARNING, f"{p.name} should be reordered",
                f"{_position_text(p)}, less than lead time + review period "
                f"({lead + p.business.review_period_days} days). Include it in this purchasing review.")
    if p.stock_risk == Product.Risk.OVERSTOCK:
        return (A.OVERSTOCK, f"over:{p.pk}", Alert.Severity.INFO, f"{p.name} is overstocked",
                f"Stock covers about {cover:.0f} days of forecast demand, above the "
                f"{p.business.overstock_cover_days}-day target. Consider pausing orders or promoting it.")
    return None


def sync_product_stock_alert(product: Product) -> None:
    """Called after each stock movement so risk alerts react in real time."""
    spec = _stock_alert_spec(product)
    keys = []
    if spec:
        alert_type, key, severity, title, message = spec
        raise_alert(product.business, alert_type, key, title, message, severity, product=product)
        keys.append(key)
    (
        Alert.objects.filter(product=product, alert_type__in=STOCK_TYPES, status__in=ACTIVE)
        .exclude(dedupe_key__in=keys)
        .update(status=Alert.Status.RESOLVED, resolved_at=timezone.now(), resolution_note="Stock level recovered.")
    )


def evaluate_stock_alerts(business) -> int:
    keys, raised = [], 0
    for p in Product.objects.filter(business=business, is_active=True).select_related("business", "preferred_supplier"):
        spec = _stock_alert_spec(p)
        if not spec:
            continue
        alert_type, key, severity, title, message = spec
        _, created = raise_alert(business, alert_type, key, title, message, severity, product=p)
        raised += created
        keys.append(key)
    auto_resolve(business, STOCK_TYPES, keys)
    return raised


def evaluate_expiry_alerts(business) -> int:
    today = timezone.localdate()
    horizon = today + timedelta(days=settings.INTELLIGENCE["EXPIRY_WARNING_DAYS"])
    keys, raised = [], 0
    batches = StockBatch.objects.filter(
        business=business, quantity_remaining__gt=0, expiry_date__lte=horizon
    ).select_related("product")
    for b in batches:
        p, days = b.product, b.days_to_expiry
        unit = p.unit
        remaining = f"{b.quantity_remaining:f}".rstrip("0").rstrip(".")
        key = f"expiry:{b.pk}"
        if days < 0:
            severity, title = Alert.Severity.CRITICAL, f"{p.name} batch expired"
            message = (f"Batch {b.batch_number or b.pk} expired on {b.expiry_date:%d %b %Y} with "
                       f"{remaining} {unit} remaining. Remove from sale and write off.")
        else:
            demand = float(p.forecast_daily_demand or 0)
            sellable = demand * (days + 1)  # expiry date inclusive
            at_risk = max(float(b.quantity_remaining) - sellable, 0)
            severity = Alert.Severity.WARNING if at_risk > 0 else Alert.Severity.INFO
            when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            title = f"{p.name} batch expires {when}"
            if at_risk > 0:
                outlook = (f"At the forecast rate ({demand:.1f}/day) about {at_risk:.0f} {unit} may not sell in time. "
                           "Consider a promotion, a price reduction or a transfer.")
            else:
                outlook = f"At the forecast rate ({demand:.1f}/day) it should sell through before expiry."
            message = f"{remaining} {unit} expire on {b.expiry_date:%d %b %Y}. {outlook}"
        _, created = raise_alert(business, A.EXPIRY, key, title, message, severity, product=p)
        raised += created
        keys.append(key)
    auto_resolve(business, (A.EXPIRY,), keys, note="Batch sold through or no longer at risk.")
    return raised
