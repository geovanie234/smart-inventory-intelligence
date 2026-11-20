"""
Data preparation: turn sales and ledger records into clean daily time series.

Two frames are produced, both indexed by calendar day with one column per product:

* `sales`     — units sold per day, zeros included for days without sales;
* `available` — True when the product was in stock for the whole day. Days on which
                the product opened or closed at zero are censored: recorded sales
                understate true demand, so models neither train nor score on them.
"""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.inventory.models import InventoryTransaction
from apps.sales.models import Sale, SaleItem


def _aware_start(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


def load_daily_sales(business, start: date, end: date, product_ids=None) -> pd.DataFrame:
    qs = SaleItem.objects.filter(
        sale__business=business, sale__status=Sale.Status.COMPLETED,
        sale__sold_at__date__gte=start, sale__sold_at__date__lte=end,
    )
    if product_ids is not None:
        qs = qs.filter(product_id__in=product_ids)
    rows = qs.annotate(day=TruncDate("sale__sold_at")).values("product_id", "day").annotate(q=Sum("quantity"))
    index = pd.date_range(start, end, freq="D")
    if not rows:
        return pd.DataFrame(index=index, dtype=float)
    df = pd.DataFrame.from_records(rows)
    df["day"] = pd.to_datetime(df["day"])
    df["q"] = df["q"].astype(float)
    wide = df.pivot_table(index="day", columns="product_id", values="q", aggfunc="sum")
    return wide.reindex(index).fillna(0.0)


def load_daily_closing_stock(business, start: date, end: date, product_ids=None) -> tuple[pd.DataFrame, pd.Series]:
    """Returns (closing stock per day per product, opening balance at `start`)."""
    base = InventoryTransaction.objects.filter(business=business)
    if product_ids is not None:
        base = base.filter(product_id__in=product_ids)
    opening = pd.Series(
        {r["product_id"]: float(r["s"]) for r in
         base.filter(occurred_at__lt=_aware_start(start)).values("product_id").annotate(s=Sum("quantity"))},
        dtype=float,
    )
    rows = (
        base.filter(occurred_at__date__gte=start, occurred_at__date__lte=end)
        .annotate(day=TruncDate("occurred_at")).values("product_id", "day").annotate(q=Sum("quantity"))
    )
    index = pd.date_range(start, end, freq="D")
    if rows:
        df = pd.DataFrame.from_records(rows)
        df["day"] = pd.to_datetime(df["day"])
        df["q"] = df["q"].astype(float)
        net = df.pivot_table(index="day", columns="product_id", values="q", aggfunc="sum").reindex(index).fillna(0.0)
    else:
        net = pd.DataFrame(index=index, dtype=float)
    cols = sorted(set(net.columns) | set(opening.index))
    net = net.reindex(columns=cols, fill_value=0.0)
    closing = net.cumsum() + opening.reindex(cols).fillna(0.0)
    return closing, opening.reindex(cols).fillna(0.0)


def build_demand_frames(business, start: date, end: date, product_ids=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    sales = load_daily_sales(business, start, end, product_ids)
    closing, opening = load_daily_closing_stock(business, start, end, product_ids)
    cols = sorted(set(sales.columns) | set(closing.columns) | set(product_ids or []))
    sales = sales.reindex(columns=cols, fill_value=0.0)
    closing = closing.reindex(columns=cols, fill_value=0.0)
    opening = opening.reindex(cols).fillna(0.0)
    opened_with = closing.shift(1)
    opened_with.iloc[0] = opening
    available = (opened_with > 0) & (closing > 0)
    return sales, available


def series_for(product_id, sales: pd.DataFrame, available: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Product series trimmed to start at the product's first stock or sale."""
    y = sales[product_id] if product_id in sales.columns else pd.Series(0.0, index=sales.index)
    av = available[product_id] if product_id in available.columns else pd.Series(False, index=sales.index)
    active = (y > 0) | av
    if not active.any():
        return y.iloc[0:0], av.iloc[0:0]
    first = active.idxmax()
    return y.loc[first:], av.loc[first:]
