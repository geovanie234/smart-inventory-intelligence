"""Small helpers to build test data quickly."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Business, User
from apps.inventory.models import Category, InventoryTransaction, Product, Supplier
from apps.inventory.services import post_transaction
from apps.sales.services import SaleLine, record_sale

_counter = {"n": 0}


def _next():
    _counter["n"] += 1
    return _counter["n"]


def business(**kw):
    n = _next()
    kw.setdefault("name", f"Test Shop {n}")
    return Business.objects.create(**kw)


def user(biz, role=User.Role.MANAGER, **kw):
    n = _next()
    return User.objects.create_user(kw.pop("username", f"user{n}"), password="pass12345!", business=biz, role=role, **kw)


def supplier(biz, lead=3, **kw):
    return Supplier.objects.create(business=biz, name=kw.pop("name", f"Supplier {_next()}"), lead_time_days=lead, **kw)


def product(biz, stock=0, **kw):
    n = _next()
    kw.setdefault("name", f"Product {n}")
    kw.setdefault("sku", f"SKU-{n}")
    kw.setdefault("cost_price", Decimal("100"))
    kw.setdefault("selling_price", Decimal("150"))
    p = Product.objects.create(business=biz, **kw)
    if stock:
        post_transaction(p, InventoryTransaction.Type.OPENING, Decimal(stock),
                         occurred_at=kw.get("opened_at") or timezone.now() - timedelta(days=400))
    return p


def at(day, hour=12):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def daily_sales(biz, prod, quantities, end_day=None):
    """Record one sale per day for the given quantities, ending yesterday."""
    end_day = end_day or timezone.localdate() - timedelta(days=1)
    start = end_day - timedelta(days=len(quantities) - 1)
    for i, q in enumerate(quantities):
        if q > 0:
            record_sale(biz, [SaleLine(prod, Decimal(int(q)))], sold_at=at(start + timedelta(days=i)))
