from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.inventory.models import InventoryTransaction, Product
from apps.inventory.services import InsufficientStockError, post_transaction, reconcile_product, record_stock_count
from apps.sales.models import Sale
from apps.sales.services import SaleLine, record_sale, void_sale

from . import factories as f

T = InventoryTransaction.Type


class LedgerTests(TestCase):
    def setUp(self):
        self.biz = f.business()
        self.p = f.product(self.biz, stock=50)

    def test_balance_follows_ledger(self):
        post_transaction(self.p, T.PURCHASE, 20)
        post_transaction(self.p, T.DAMAGE, 5)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("65"))
        last = self.p.transactions.order_by("-id").first()
        self.assertEqual(last.quantity, Decimal("-5"))  # sign derived from the movement type
        self.assertEqual(last.balance_after, Decimal("65"))

    def test_outflow_cannot_go_negative(self):
        with self.assertRaises(InsufficientStockError):
            post_transaction(self.p, T.LOSS, 51)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("50"))

    def test_zero_quantity_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventoryTransaction.objects.create(business=self.biz, product=self.p, txn_type=T.ADJUSTMENT_IN,
                                                quantity=0, balance_after=50)

    def test_stock_count_posts_correction(self):
        count = record_stock_count(self.p, 47)
        self.assertEqual(count.variance, Decimal("-3"))
        self.assertEqual(count.transaction.txn_type, T.COUNT_CORRECTION)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("47"))

    def test_reconcile_repairs_cached_balance(self):
        Product.objects.filter(pk=self.p.pk).update(stock_on_hand=999)
        previous, ledger = reconcile_product(self.p)
        self.assertEqual((previous, ledger), (Decimal("999"), Decimal("50")))
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("50"))


class SalesTests(TestCase):
    def setUp(self):
        self.biz = f.business()
        self.a = f.product(self.biz, stock=10, selling_price=Decimal("500"))
        self.b = f.product(self.biz, stock=2, selling_price=Decimal("1000"))

    def test_sale_reduces_stock_and_totals(self):
        sale = record_sale(self.biz, [SaleLine(self.a, Decimal(3)), SaleLine(self.b, Decimal(1))])
        self.assertEqual(sale.total_amount, Decimal("2500"))
        self.assertTrue(sale.reference.startswith("S-"))
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock_on_hand, Decimal("7"))

    def test_duplicate_scans_are_merged(self):
        sale = record_sale(self.biz, [SaleLine(self.a, Decimal(1)), SaleLine(self.a, Decimal(2))])
        self.assertEqual(sale.items.count(), 1)
        self.assertEqual(sale.items.get().quantity, Decimal("3"))

    def test_sale_is_atomic(self):
        with self.assertRaises(ValidationError):
            record_sale(self.biz, [SaleLine(self.a, Decimal(1)), SaleLine(self.b, Decimal(5))])
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock_on_hand, Decimal("10"))  # first line rolled back
        self.assertFalse(Sale.objects.exists())

    def test_cannot_sell_other_business_product(self):
        other = f.product(f.business(), stock=5)
        with self.assertRaises(ValidationError):
            record_sale(self.biz, [SaleLine(other, Decimal(1))])

    def test_void_returns_stock(self):
        sale = record_sale(self.biz, [SaleLine(self.a, Decimal(4))])
        void_sale(sale, reason="Wrong item")
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock_on_hand, Decimal("10"))
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.VOIDED)
        with self.assertRaises(ValidationError):
            void_sale(sale)
