from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.inventory.models import StockBatch
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.services import approve_order, create_order, on_order_quantity, receive_items, submit_order

from . import factories as f

S = PurchaseOrder.Status


class PurchasingWorkflowTests(TestCase):
    def setUp(self):
        self.biz = f.business()
        self.manager = f.user(self.biz, User.Role.MANAGER)
        self.buyer = f.user(self.biz, User.Role.PURCHASING)
        self.sup = f.supplier(self.biz, lead=4)
        self.p = f.product(self.biz, stock=5, preferred_supplier=self.sup)

    def _order(self, qty=20):
        return create_order(self.biz, self.sup, [(self.p, Decimal(qty), Decimal("90"))], user=self.buyer)

    def test_only_managers_approve(self):
        order = self._order()
        submit_order(order, self.buyer)
        with self.assertRaises(ValidationError):
            approve_order(order, self.buyer)
        approve_order(order, self.manager)
        self.assertEqual(order.status, S.APPROVED)
        self.assertEqual(order.expected_delivery_date, timezone.localdate() + timedelta(days=4))

    def test_cannot_receive_unapproved_order(self):
        order = self._order()
        with self.assertRaises(ValidationError):
            receive_items(order, {order.items.get().pk: {"quantity": 5}})

    def test_partial_then_full_receipt(self):
        order = self._order(20)
        approve_order(order, self.manager)
        item = order.items.get()
        self.assertEqual(on_order_quantity(self.p), Decimal("20"))
        receive_items(order, {item.pk: {"quantity": Decimal("8")}})
        order.refresh_from_db()
        self.assertEqual(order.status, S.PARTIAL)
        self.assertEqual(on_order_quantity(self.p), Decimal("12"))
        receive_items(order, {item.pk: {"quantity": Decimal("12")}})
        order.refresh_from_db()
        self.p.refresh_from_db()
        self.assertEqual(order.status, S.RECEIVED)
        self.assertEqual(self.p.stock_on_hand, Decimal("25"))
        self.assertEqual(self.p.cost_price, Decimal("90"))  # latest purchase cost

    def test_over_receipt_rejected(self):
        order = self._order(10)
        approve_order(order, self.manager)
        with self.assertRaises(ValidationError):
            receive_items(order, {order.items.get().pk: {"quantity": Decimal("12")}})

    def test_expiry_tracked_products_need_batch_dates(self):
        self.p.track_expiry = True
        self.p.save()
        order = self._order(10)
        approve_order(order, self.manager)
        item = order.items.get()
        with self.assertRaises(ValidationError):
            receive_items(order, {item.pk: {"quantity": Decimal("10")}})
        expiry = timezone.localdate() + timedelta(days=10)
        receive_items(order, {item.pk: {"quantity": Decimal("10"), "expiry_date": expiry, "batch_number": "L1"}})
        batch = StockBatch.objects.get(product=self.p)
        self.assertEqual(batch.quantity_remaining, Decimal("10"))
