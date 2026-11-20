"""Smoke tests: every screen renders for each role, and role restrictions hold."""
from decimal import Decimal

import numpy as np
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.intelligence.services.pipeline import run_intelligence
from apps.iot.models import IoTDevice
from apps.purchasing.services import create_order
from apps.sales.models import Sale

from . import factories as f


class ScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.biz = f.business(is_demo=True)
        cls.manager = f.user(cls.biz, User.Role.MANAGER)
        cls.clerk = f.user(cls.biz, User.Role.CLERK)
        sup = f.supplier(cls.biz)
        cls.p = f.product(cls.biz, stock=1500, preferred_supplier=sup, barcode="600123")
        f.daily_sales(cls.biz, cls.p, list(np.random.default_rng(3).poisson(9, 70)))
        cls.order = create_order(cls.biz, sup, [(cls.p, Decimal(10), None)], user=cls.manager)
        cls.device = IoTDevice(business=cls.biz, name="Scale", device_id="s-1", product=cls.p, unit_weight_grams=1000)
        cls.device.rotate_key()
        cls.device.save()
        run_intelligence(cls.biz)

    def test_manager_sees_every_screen(self):
        self.client.force_login(self.manager)
        sale = Sale.objects.first()
        urls = [
            reverse("dashboard:home"), reverse("inventory:product_list"), reverse("inventory:product_detail", args=[self.p.pk]),
            reverse("inventory:product_create"), reverse("inventory:category_list"), reverse("inventory:supplier_list"),
            reverse("inventory:supplier_detail", args=[self.p.preferred_supplier_id]), reverse("inventory:ledger"),
            reverse("inventory:adjust"), reverse("inventory:stock_count"), reverse("inventory:batch_list"),
            reverse("sales:sale_list"), reverse("sales:sale_create"), reverse("sales:report"),
            reverse("sales:sale_detail", args=[sale.pk]), reverse("purchasing:po_list"), reverse("purchasing:po_create"),
            reverse("purchasing:po_detail", args=[self.order.pk]), reverse("intelligence:forecasts"),
            reverse("intelligence:recommendations"), reverse("intelligence:alerts"), reverse("intelligence:evaluation"),
            reverse("iot:device_list"), reverse("iot:device_detail", args=[self.device.pk]), reverse("iot:device_create"),
            reverse("accounts:staff_list"), reverse("accounts:business_settings"),
            reverse("inventory:product_list") + "?export=csv", reverse("sales:report") + "?export=csv",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse("dashboard:home"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("accounts:login"), r["Location"])

    def test_clerk_restrictions(self):
        self.client.force_login(self.clerk)
        self.assertEqual(self.client.get(reverse("sales:sale_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventory:product_create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:staff_list")).status_code, 403)
        r = self.client.post(reverse("purchasing:po_action", args=[self.order.pk, "approve"]))
        self.assertEqual(r.status_code, 403)

    def test_record_sale_through_ui(self):
        self.client.force_login(self.clerk)
        before = self.p.__class__.objects.get(pk=self.p.pk).stock_on_hand
        r = self.client.post(reverse("sales:sale_create"), {
            "product": [self.p.pk], "quantity": ["2"], "unit_price": [""], "payment_method": "momo",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.p.__class__.objects.get(pk=self.p.pk).stock_on_hand, before - 2)

    def test_barcode_lookup(self):
        self.client.force_login(self.clerk)
        r = self.client.get(reverse("inventory:product_lookup"), {"code": "600123"})
        self.assertEqual(r.json()["id"], self.p.pk)
        self.assertEqual(self.client.get(reverse("inventory:product_lookup"), {"code": "nope"}).status_code, 404)
