"""
Explainable reorder recommendations (periodic-review, order-up-to policy).

    expected demand  = forecast daily demand × (lead time + review period)
    safety stock     = configured value, or z × σ(forecast error) × √(lead time + review period)
    target level     = expected demand + safety stock
    order quantity   = target level − stock on hand − quantity already on order
    → then minimum order quantity, pack-size rounding and shelf capacity are applied.

This mirrors the worked example in the project proposal (§10):
8 × (3 + 7) = 80;  80 + 10 − 30 = 60 units.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.inventory.models import Product
from apps.purchasing.services import on_order_map

from ..models import ReorderRecommendation


def _fmt(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


@dataclass
class ReorderCalculation:
    current_stock: float
    on_order: float
    avg_daily_demand: float
    demand_std: float
    lead_time_days: int
    review_period_days: int
    safety_stock: float
    safety_stock_method: str
    expected_demand: float
    target_level: float
    raw_quantity: float
    recommended_quantity: float
    days_of_cover: float | None
    steps: list[str] = field(default_factory=list)


def unsellable_before_expiry(batches, avg_daily_demand: float) -> float:
    """
    Estimate stock that will expire unsold, selling first-expiry-first-out at the
    forecast rate. `batches`: iterable of (quantity_remaining, days_to_expiry).
    """
    sold_days, waste = 0.0, 0.0
    for remaining, days in sorted(batches, key=lambda b: b[1]):
        remaining = float(remaining)
        sell_window = max(days + 1, 0)  # the expiry date itself is still a selling day
        capacity = max(avg_daily_demand * (sell_window - sold_days), 0.0)
        sold = min(remaining, capacity)
        waste += remaining - sold
        sold_days += sold / avg_daily_demand if avg_daily_demand > 0 else 0
    return waste


def calculate_reorder(
    *, current_stock: float, avg_daily_demand: float, lead_time_days: int, review_period_days: int,
    safety_stock: float | None = None, demand_std: float = 0.0, service_z: float | None = None,
    on_order: float = 0.0, pack_size: float = 1.0, min_order_qty: float = 0.0, shelf_capacity: float | None = None,
    unsellable: float = 0.0,
) -> ReorderCalculation:
    z = settings.INTELLIGENCE["SERVICE_LEVEL_Z"] if service_z is None else service_z
    period = lead_time_days + review_period_days
    expected = avg_daily_demand * period
    steps = [
        f"Expected demand over lead time + review period = {_fmt(avg_daily_demand)}/day × "
        f"({lead_time_days} + {review_period_days}) days = {_fmt(expected)}"
    ]
    if unsellable > 0:
        usable = max(current_stock - unsellable, 0.0)
        steps.append(
            f"Usable stock = on hand {_fmt(current_stock)} − {_fmt(unsellable)} expected to expire unsold "
            f"= {_fmt(usable)}"
        )
        current_stock = usable
    if safety_stock is None:
        safety_stock = z * demand_std * math.sqrt(period)
        method = "computed"
        steps.append(
            f"Safety stock = z {z} × forecast error σ {_fmt(demand_std)} × √{period} = {_fmt(safety_stock)}"
        )
    else:
        method = "configured"
        steps.append(f"Safety stock (configured) = {_fmt(safety_stock)}")
    target = expected + safety_stock
    raw = target - current_stock - on_order
    on_order_txt = f" − on order {_fmt(on_order)}" if on_order else ""
    stock_label = "usable stock" if unsellable > 0 else "on hand"
    steps.append(
        f"Order quantity = expected {_fmt(expected)} + safety {_fmt(safety_stock)} − {stock_label} "
        f"{_fmt(current_stock)}{on_order_txt} = {_fmt(raw)}"
    )

    qty = max(raw, 0.0)
    if qty > 0:
        if min_order_qty and qty < min_order_qty:
            steps.append(f"Raised to supplier minimum order quantity {_fmt(min_order_qty)}")
            qty = min_order_qty
        if pack_size and pack_size > 0:
            rounded = round(math.ceil(qty / pack_size - 1e-9) * pack_size, 3)
            if rounded != qty and pack_size != 1:
                steps.append(f"Rounded up to full packs of {_fmt(pack_size)} = {_fmt(rounded)}")
            qty = rounded
        if shelf_capacity is not None:
            room = max(shelf_capacity - current_stock - on_order, 0.0)
            if qty > room:
                capped = math.floor(room / (pack_size or 1)) * (pack_size or 1)
                steps.append(f"Capped by shelf capacity {_fmt(shelf_capacity)} to {_fmt(capped)}")
                qty = capped
    else:
        steps.append("Stock on hand and on order already cover the target level — no order needed.")

    cover = current_stock / avg_daily_demand if avg_daily_demand > 0 else None
    return ReorderCalculation(
        current_stock=current_stock + unsellable if unsellable > 0 else current_stock, on_order=on_order, avg_daily_demand=avg_daily_demand, demand_std=demand_std,
        lead_time_days=lead_time_days, review_period_days=review_period_days, safety_stock=safety_stock,
        safety_stock_method=method, expected_demand=expected, target_level=target, raw_quantity=raw,
        recommended_quantity=float(qty), days_of_cover=cover, steps=steps,
    )


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 3)))


@transaction.atomic
def generate_recommendations(business, run=None, demand_stats: dict | None = None) -> list[ReorderRecommendation]:
    """
    demand_stats: {product_id: (avg_daily_demand, forecast_error_std)} from the forecasting run.
    Supersedes earlier pending recommendations so only the latest advice is actionable.
    """
    demand_stats = demand_stats or {}
    on_order = on_order_map(business)
    created = []
    products = Product.objects.filter(business=business, is_active=True).select_related("preferred_supplier", "business")
    for p in products:
        if p.pk in demand_stats:
            avg, std = demand_stats[p.pk]
        elif p.forecast_daily_demand is not None:
            avg, std = float(p.forecast_daily_demand), 0.0
        else:
            continue
        unsellable = 0.0
        if p.track_expiry and avg > 0:
            unsellable = unsellable_before_expiry(
                [(b.quantity_remaining, b.days_to_expiry) for b in p.batches.filter(quantity_remaining__gt=0)],
                float(avg),
            )
        calc = calculate_reorder(
            unsellable=min(unsellable, float(p.stock_on_hand)),
            current_stock=float(p.stock_on_hand), avg_daily_demand=float(avg), demand_std=float(std),
            lead_time_days=p.effective_lead_time, review_period_days=business.review_period_days,
            safety_stock=float(p.safety_stock) if p.safety_stock is not None else None,
            on_order=float(on_order.get(p.pk, 0)), pack_size=float(p.pack_size or 1),
            min_order_qty=float(p.min_order_qty or 0),
            shelf_capacity=float(p.shelf_capacity) if p.shelf_capacity is not None else None,
        )
        ReorderRecommendation.objects.filter(product=p, status=ReorderRecommendation.Status.PENDING).update(
            status=ReorderRecommendation.Status.SUPERSEDED, decided_at=timezone.now()
        )
        if calc.recommended_quantity <= 0:
            continue
        created.append(ReorderRecommendation.objects.create(
            business=business, run=run, product=p, supplier=p.preferred_supplier,
            risk_level=p.compute_risk(on_order.get(p.pk, 0)),
            current_stock=_d(calc.current_stock), on_order=_d(calc.on_order),
            avg_daily_demand=_d(calc.avg_daily_demand), demand_std=_d(calc.demand_std),
            lead_time_days=calc.lead_time_days, review_period_days=calc.review_period_days,
            safety_stock=_d(calc.safety_stock), safety_stock_method=calc.safety_stock_method,
            expected_demand=_d(calc.expected_demand), target_level=_d(calc.target_level),
            raw_quantity=_d(calc.raw_quantity), recommended_quantity=_d(calc.recommended_quantity),
            days_of_cover=calc.days_of_cover,
            estimated_cost=Decimal(str(round(calc.recommended_quantity * float(p.cost_price), 2))),
            explanation=calc.steps,
        ))
    return created
