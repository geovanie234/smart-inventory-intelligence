"""
Generate a CLEARLY LABELLED SYNTHETIC demo business for development and demonstration.

Synthetic data exercises the full pipeline (ledger, purchasing, forecasting,
alerts, IoT) but cannot prove model performance in real Kigali shops (proposal §12).

    python manage.py seed_demo            # ~240 days of history, then runs the intelligence cycle
    python manage.py seed_demo --reset    # delete and recreate the demo business
"""
import math
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

import numpy as np
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Business, User
from apps.intelligence.ml.calendar import is_holiday
from apps.inventory.models import Category, InventoryTransaction, Product, StockBatch, StockCount, Supplier
from apps.inventory.services import post_transaction, record_stock_count
from apps.iot.models import IoTDevice
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import create_order, receive_items
from apps.sales.models import Sale
from apps.sales.services import SaleLine, record_sale

DEMO_NAME = "Kigali Fresh Mart (DEMO · synthetic data)"
PASSWORD = "Demo@2026"
T = InventoryTransaction.Type

SUPPLIERS = [
    ("Nyabugogo Wholesale Ltd", "Jean Mugabo", "+250 788 100 201", 2),
    ("Kimironko Fresh Supplies", "Aline Uwase", "+250 788 100 202", 1),
    ("Rwanda Dairy Distributors", "Eric Habimana", "+250 788 100 203", 1),
    ("Gikondo Household Traders", "Grace Mukamana", "+250 788 100 204", 4),
    ("Remera Beverage Depot", "Patrick Niyonzima", "+250 788 100 205", 3),
]
# name, sku, category, unit, cost, price, base/day, supplier, pack, shelf_life_days (None = no expiry)
PRODUCTS = [
    ("Mineral water 500ml", "BEV-001", "Beverages", "bottle", 250, 400, 38, 4, 24, None),
    ("Mineral water 1.5L", "BEV-002", "Beverages", "bottle", 450, 700, 16, 4, 12, None),
    ("Soda 300ml", "BEV-003", "Beverages", "bottle", 300, 500, 32, 4, 24, None),
    ("Orange juice 1L", "BEV-004", "Beverages", "pc", 1200, 1800, 6, 4, 12, None),
    ("Energy drink 250ml", "BEV-005", "Beverages", "pc", 700, 1000, 7, 4, 24, None),
    ("Fresh milk 500ml", "DAI-001", "Dairy", "pc", 450, 600, 28, 2, 20, 7),
    ("Yoghurt 500ml", "DAI-002", "Dairy", "pc", 900, 1300, 9, 2, 12, 21),
    ("Butter 250g", "DAI-003", "Dairy", "pc", 2200, 3000, 3, 2, 10, 60),
    ("White bread loaf", "BAK-001", "Bakery", "pc", 900, 1200, 24, 1, 10, 5),
    ("Chapati pack (5)", "BAK-002", "Bakery", "pack", 800, 1100, 11, 1, 10, 4),
    ("Rice 5kg bag", "STA-001", "Staples", "bag", 6500, 8000, 7, 0, 4, None),
    ("Maize flour 2kg", "STA-002", "Staples", "pack", 1800, 2300, 10, 0, 10, None),
    ("Sugar 1kg", "STA-003", "Staples", "pack", 1300, 1600, 14, 0, 20, None),
    ("Loose sugar (kg)", "STA-004", "Staples", "kg", 1250, 1550, 11, 0, 50, None),
    ("Cooking oil 1L", "STA-005", "Staples", "bottle", 2600, 3200, 9, 0, 12, None),
    ("Beans 1kg", "STA-006", "Staples", "pack", 1100, 1400, 12, 0, 25, None),
    ("Salt 500g", "STA-007", "Staples", "pack", 250, 400, 6, 0, 25, None),
    ("Laundry soap bar", "HOU-001", "Household", "pc", 500, 700, 13, 3, 25, None),
    ("Detergent powder 1kg", "HOU-002", "Household", "pack", 2000, 2600, 5, 3, 12, None),
    ("Toilet paper 4-pack", "HOU-003", "Household", "pack", 1500, 2000, 6, 3, 10, None),
    ("Charcoal bag", "HOU-004", "Household", "bag", 5000, 6500, 3, 3, 1, None),
    ("Toothpaste 100ml", "PER-001", "Personal care", "pc", 1100, 1500, 5, 3, 12, None),
    ("Bath soap 150g", "PER-002", "Personal care", "pc", 600, 850, 8, 3, 24, None),
    ("Body lotion 400ml", "PER-003", "Personal care", "bottle", 2800, 3600, 2, 3, 12, None),
    ("Tomatoes (kg)", "FRE-001", "Fresh produce", "kg", 700, 1000, 14, 1, 1, 6),
    ("Irish potatoes (kg)", "FRE-002", "Fresh produce", "kg", 450, 650, 24, 1, 1, None),
    ("Onions (kg)", "FRE-003", "Fresh produce", "kg", 800, 1100, 9, 1, 1, None),
]
DOW = [0.95, 0.9, 0.95, 1.0, 1.15, 1.35, 0.8]  # Mon..Sun


class Command(BaseCommand):
    help = "Create a demo business filled with clearly labelled synthetic data."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=240)
        parser.add_argument("--reset", action="store_true", help="Delete an existing demo business first")
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument("--skip-intelligence", action="store_true")

    def handle(self, *args, **opts):
        existing = Business.objects.filter(name=DEMO_NAME).first()
        if existing:
            if not opts["reset"]:
                self.stdout.write(self.style.WARNING("Demo business already exists. Use --reset to recreate it."))
                return
            self._delete(existing)
        self.rng = random.Random(opts["seed"])
        self.np_rng = np.random.default_rng(opts["seed"])
        business, users = self._setup()
        self._simulate(business, users, opts["days"])
        self._iot(business)
        if not opts["skip_intelligence"]:
            from apps.intelligence.services.pipeline import run_intelligence

            self.stdout.write("Running forecasting and alert cycle…")
            result = run_intelligence(business, user=users["manager"])
            s = result["run"].summary
            self.stdout.write(f"  WAPE baseline {s.get('baseline_wape') or 0:.1f}% → selected {s.get('selected_wape') or 0:.1f}%; "
                              f"{result['recommendations']} recommendations")
        self.stdout.write(self.style.SUCCESS(
            f"\nDemo ready: {business.name}\n"
            f"  Sign in at /accounts/login/ with any of: {' / '.join(self.usernames)}  —  password: {PASSWORD}\n"
            "  (Demo credentials only. Never use these in production.)"
        ))

    # ------------------------------------------------------------------ #
    def _delete(self, business):
        from apps.intelligence.models import Alert, ForecastRun, ReorderRecommendation
        from apps.iot.models import IoTReading
        from apps.sales.models import SaleItem

        self.stdout.write("Removing previous demo business…")
        with transaction.atomic():
            Alert.objects.filter(business=business).delete()
            ReorderRecommendation.objects.filter(business=business).delete()
            ForecastRun.objects.filter(business=business).delete()
            IoTReading.objects.filter(business=business).delete()
            IoTDevice.objects.filter(business=business).delete()
            StockBatch.objects.filter(business=business).delete()
            StockCount.objects.filter(business=business).delete()
            InventoryTransaction.objects.filter(business=business).delete()
            SaleItem.objects.filter(sale__business=business).delete()
            Sale.objects.filter(business=business).delete()
            PurchaseOrder.objects.filter(business=business).delete()
            Product.objects.filter(business=business).delete()
            Supplier.objects.filter(business=business).delete()
            Category.objects.filter(business=business).delete()
            User.objects.filter(business=business).delete()
            business.delete()

    def _setup(self):
        business = Business.objects.create(
            name=DEMO_NAME, business_type=Business.BusinessType.SUPERMARKET, address="KG 11 Ave, Kimironko, Kigali",
            phone="+250 788 000 000", currency="RWF", review_period_days=7, default_lead_time_days=3, is_demo=True,
        )
        users = {}
        for username, first, last, role in [
            ("manager", "Diane", "Ingabire", User.Role.MANAGER),
            ("buyer", "Olivier", "Nshimiyimana", User.Role.PURCHASING),
            ("clerk", "Claudine", "Uwimana", User.Role.CLERK),
        ]:
            if User.objects.filter(username=username).exists():
                username = f"{username}_{business.pk}"
            users[role] = User.objects.create_user(username, password=PASSWORD, first_name=first, last_name=last,
                                                   business=business, role=role)
        self.usernames = [u.username for u in users.values()]
        users["manager"] = users[User.Role.MANAGER]
        cats = {n: Category.objects.create(business=business, name=n) for n in {p[2] for p in PRODUCTS}}
        sups = [Supplier.objects.create(business=business, name=n, contact_person=c, phone=ph, lead_time_days=lt)
                for n, c, ph, lt in SUPPLIERS]
        self.specs = []
        for i, (name, sku, cat, unit, cost, price, base, sup, pack, shelf) in enumerate(PRODUCTS):
            p = Product.objects.create(
                business=business, name=name, sku=sku, barcode=f"6{(10_000_000_000 + i * 7919):011d}"[:12],
                category=cats[cat], unit=unit, cost_price=Decimal(cost), selling_price=Decimal(price),
                preferred_supplier=sups[sup], pack_size=Decimal(pack), track_expiry=shelf is not None,
            )
            self.specs.append({
                "product": p, "base": base, "shelf": shelf, "lead": sups[sup].lead_time_days,
                "trend": self.rng.uniform(-0.15, 0.3), "phase": self.rng.uniform(0, 2 * math.pi),
            })
        self.stdout.write(f"Created {len(self.specs)} products, {len(sups)} suppliers, 3 users.")
        return business, users

    def _demand(self, spec, day, idx, total_days):
        f = spec["base"] * DOW[day.weekday()]
        if day.day >= 25 or day.day <= 3:
            f *= 1.2
        if is_holiday(day):
            f *= 0.65
        elif is_holiday(day + timedelta(days=1)):
            f *= 1.25
        f *= 1 + spec["trend"] * idx / total_days
        f *= 1 + 0.12 * math.sin(2 * math.pi * day.timetuple().tm_yday / 365 + spec["phase"])
        return int(self.np_rng.poisson(max(f, 0.1)))

    def _at(self, day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)))

    def _simulate(self, business, users, days):
        manager, clerk, buyer = users["manager"], users[User.Role.CLERK], users[User.Role.PURCHASING]
        today = timezone.localdate()
        start = today - timedelta(days=days)
        pending = []  # (arrival_day, order, {item_id: qty}, expiry by item)
        self.stdout.write(f"Simulating {days} days of trading from {start}…")

        with transaction.atomic():
            for s in self.specs:
                p = s["product"]
                qty = Decimal(math.ceil(s["base"] * (s["lead"] + 10)))
                post_transaction(p, T.OPENING, qty, user=manager, occurred_at=self._at(start - timedelta(days=1), 18),
                                 notes="Opening balance (synthetic)", refresh_risk=False, unit_cost=p.cost_price)
                if p.track_expiry:
                    StockBatch.objects.create(business=business, product=p, expiry_date=start + timedelta(days=s["shelf"]),
                                              quantity_received=qty, quantity_remaining=qty, batch_number="OPEN")

        for idx in range(days):
            day = start + timedelta(days=idx)
            with transaction.atomic():
                # 1. deliveries arriving today (07:30)
                for entry in [e for e in pending if e[0] <= day]:
                    pending.remove(entry)
                    _, order, receipts = entry
                    receive_items(order, receipts, user=clerk, received_at=self._at(day, 7, 30))

                # 2. today's sales, split across several customer baskets
                n_baskets = self.rng.randint(14, 26)
                baskets = [[] for _ in range(n_baskets)]
                for s in self.specs:
                    p = s["product"]
                    p.refresh_from_db(fields=["stock_on_hand"])
                    want = self._demand(s, day, idx, days)
                    qty = min(Decimal(want), p.stock_on_hand)  # lost sales when stock runs out
                    if qty <= 0:
                        continue
                    parts = min(int(qty), self.rng.randint(1, 4))
                    shares = self.np_rng.multinomial(int(qty), [1 / parts] * parts)
                    for share in shares:
                        if share > 0:
                            baskets[self.rng.randrange(n_baskets)].append(SaleLine(p, Decimal(int(share))))
                for b_idx, lines in enumerate(baskets):
                    if not lines:
                        continue
                    hour = self.rng.choice([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 17, 18, 18, 19, 20])
                    record_sale(business, lines, user=clerk, sold_at=self._at(day, hour, self.rng.randint(0, 59)),
                                payment_method=self.rng.choice(["cash", "cash", "momo", "momo", "momo", "card"]),
                                refresh_risk=False)

                # 3. write-offs: expired batches, occasional damage / loss
                for batch in StockBatch.objects.filter(business=business, quantity_remaining__gt=0, expiry_date__lt=day):
                    post_transaction(batch.product, T.EXPIRED, batch.quantity_remaining, user=clerk,
                                     occurred_at=self._at(day, 20), notes=f"Expired batch {batch.batch_number}",
                                     refresh_risk=False, allow_negative=True)
                for s in self.specs:
                    p = s["product"]
                    if self.rng.random() >= 0.02:
                        continue
                    p.refresh_from_db(fields=["stock_on_hand"])
                    if p.stock_on_hand > 2:
                        post_transaction(p, self.rng.choice([T.DAMAGE, T.LOSS]), Decimal(self.rng.randint(1, 2)),
                                         user=clerk, occurred_at=self._at(day, 19), notes="Routine write-off",
                                         refresh_risk=False)

                # 4. monthly stock count of a sample of products (small shrinkage found)
                if idx % 30 == 15:
                    for s in self.rng.sample(self.specs, 8):
                        p = s["product"]
                        p.refresh_from_db(fields=["stock_on_hand"])
                        drift = Decimal(-self.rng.choice([0, 0, 0, 1, 1, 2]))
                        record_stock_count(p, max(p.stock_on_hand + drift, Decimal(0)), user=clerk,
                                           counted_at=self._at(day, 21), notes="Monthly cycle count")

                # 5. buyer reviews stock each evening (simple min/max policy, sometimes late)
                open_products = {i.product_id for _, o, _ in pending for i in o.items.all()}
                by_supplier = {}
                for s in self.specs:
                    p = s["product"]
                    p.refresh_from_db(fields=["stock_on_hand"])
                    if p.pk in open_products or self.rng.random() < 0.1:  # 10%: reorder forgotten → stockouts
                        continue
                    if p.stock_on_hand <= s["base"] * (s["lead"] + 2):
                        target = s["base"] * (s["lead"] + 12)
                        qty = max(Decimal(math.ceil(target)) - p.stock_on_hand, Decimal(1))
                        by_supplier.setdefault(p.preferred_supplier, []).append((p, qty, p.cost_price))
                for supplier, lines in by_supplier.items():
                    order = create_order(business, supplier, lines, user=buyer)
                    ordered_at = self._at(day, 18, 30)
                    PurchaseOrder.objects.filter(pk=order.pk).update(
                        status=PurchaseOrder.Status.APPROVED, approved_by=manager, approved_at=ordered_at,
                        submitted_at=ordered_at, expected_delivery_date=day + timedelta(days=supplier.lead_time_days),
                        created_at=ordered_at,
                    )
                    order.refresh_from_db()
                    delay = supplier.lead_time_days + self.rng.choice([0, 0, 0, 0, 1, 1, 2])
                    arrival = day + timedelta(days=delay)
                    receipts = {}
                    for item in order.items.select_related("product"):
                        spec = next(sp for sp in self.specs if sp["product"].pk == item.product_id)
                        receipts[item.pk] = {
                            "quantity": item.quantity_ordered,
                            "expiry_date": arrival + timedelta(days=spec["shelf"]) if spec["shelf"] else None,
                            "batch_number": f"L{arrival:%y%m%d}",
                        }
                    if arrival < today:
                        pending.append((arrival, order, receipts))

            if idx % 30 == 0:
                self.stdout.write(f"  … {day}")

        # Leave some orders open (arrive in the future) — they count as "on order".
        # Inject a few suspicious recent movements for the anomaly detector to find.
        with transaction.atomic():
            for s in self.rng.sample(self.specs, 3):
                p = s["product"]
                p.refresh_from_db()
                qty = min(p.stock_on_hand, Decimal(max(int(s["base"] * 4), 8)))
                if qty > 0:
                    post_transaction(p, T.LOSS, qty, user=clerk, occurred_at=timezone.now() - timedelta(days=1, hours=3),
                                     notes="Unexplained shortage (synthetic anomaly)", refresh_risk=False)
        from apps.intelligence.services.anomalies import screen_transaction

        for txn in InventoryTransaction.objects.filter(business=business, notes__contains="synthetic anomaly"):
            screen_transaction(txn)
        for p in Product.objects.filter(business=business):
            p.stock_risk = p.compute_risk()
            p.save(update_fields=["stock_risk"])
        self.stdout.write(f"  {Sale.objects.filter(business=business).count():,} sales, "
                          f"{InventoryTransaction.objects.filter(business=business).count():,} ledger entries, "
                          f"{PurchaseOrder.objects.filter(business=business).count():,} purchase orders.")

    def _iot(self, business):
        from apps.iot.services import ingest_reading

        product = Product.objects.get(business=business, sku="STA-004")
        device = IoTDevice(
            business=business, name="Bulk sugar scale", device_id=f"sugar-scale-{business.pk:02d}",
            device_type=IoTDevice.DeviceType.LOAD_CELL, product=product, location="Shop floor, bulk bin 1",
            unit_weight_grams=Decimal("1000"), tare_grams=Decimal("2500"), tolerance_pct=Decimal("5"),
            expected_interval_seconds=1800,
        )
        raw_key = device.rotate_key()
        device.save()
        now = timezone.now()
        stock = float(product.stock_on_hand)
        for i in range(48, 0, -1):
            ts = now - timedelta(minutes=30 * i)
            est = stock + self.rng.gauss(0, 0.15)
            if i <= 2:
                est -= max(6.0, 0.15 * stock)  # last hour: bin ~15% lighter than records → discrepancy alert
            ingest_reading(device, {"message_id": f"seed-{i}", "measured_at": ts.isoformat(),
                                    "weight_g": round(est * 1000 + 2500, 1), "firmware": "1.0.0"})
        self.stdout.write(f"IoT demo device {device.device_id} created (key: {raw_key}) with 48 readings.")
