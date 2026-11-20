from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.intelligence.models import Alert
from apps.iot.models import IoTDevice, IoTReading
from apps.iot.services import verify_reading

from . import factories as f

URL = "/api/v1/iot/readings/"


class IoTIngestionTests(TestCase):
    def setUp(self):
        self.biz = f.business()
        self.p = f.product(self.biz, stock=20, unit="kg")
        self.device = IoTDevice(business=self.biz, name="Scale", device_id="scale-1", product=self.p,
                                unit_weight_grams=Decimal("1000"), tare_grams=Decimal("500"))
        self.key = self.device.rotate_key()
        self.device.save()
        self.client = APIClient()

    def post(self, body, key=None):
        return self.client.post(URL, body, format="json", HTTP_X_DEVICE_ID="scale-1",
                                HTTP_AUTHORIZATION=f"Device {key or self.key}")

    def test_key_is_hashed_and_checked(self):
        self.assertNotIn(self.key, self.device.key_hash)
        self.assertEqual(self.post({"message_id": "1", "weight_g": 20500}, key="wrong").status_code, 401)

    def test_matching_reading(self):
        r = self.post({"message_id": "m1", "weight_g": 20400})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], IoTReading.Status.OK)
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen_at)

    def test_duplicate_delivery_is_idempotent(self):
        self.assertEqual(self.post({"message_id": "m1", "weight_g": 20500}).status_code, 201)
        r = self.post({"message_id": "m1", "weight_g": 20500})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(IoTReading.objects.count(), 1)

    def test_discrepancy_alerts_but_never_changes_stock(self):
        r = self.post({"message_id": "m2", "weight_g": 12500})  # 12 kg vs 20 recorded
        self.assertEqual(r.json()["status"], IoTReading.Status.DISCREPANCY)
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("20"))
        self.assertTrue(Alert.objects.filter(alert_type=Alert.Type.IOT_DISCREPANCY, status=Alert.Status.OPEN).exists())
        verify_reading(IoTReading.objects.get(message_id="m2"), user=None)  # staff confirmation
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_on_hand, Decimal("12"))
        self.assertFalse(Alert.objects.filter(alert_type=Alert.Type.IOT_DISCREPANCY, status=Alert.Status.OPEN).exists())

    def test_validation(self):
        self.assertEqual(self.post({"weight_g": 100}).status_code, 400)  # no message id
        future = (timezone.now() + timedelta(hours=2)).isoformat()
        self.assertEqual(self.post({"message_id": "x", "weight_g": 100, "measured_at": future}).status_code, 400)
        r = self.post({"message_id": "neg", "weight_g": -5000})
        self.assertEqual(r.json()["status"], IoTReading.Status.INVALID)


class ApiTests(TestCase):
    def setUp(self):
        self.biz_a, self.biz_b = f.business(), f.business()
        self.clerk = f.user(self.biz_a, User.Role.CLERK)
        self.p_a = f.product(self.biz_a, stock=10, selling_price=Decimal("200"))
        self.p_b = f.product(self.biz_b, stock=10)
        self.client = APIClient()
        token = Token.objects.create(user=self.clerk)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_business_isolation(self):
        ids = [p["id"] for p in self.client.get("/api/v1/products/").json()["results"]]
        self.assertEqual(ids, [self.p_a.pk])
        self.assertEqual(self.client.get(f"/api/v1/products/{self.p_b.pk}/").status_code, 404)

    def test_clerk_records_sale_but_cannot_edit_catalog(self):
        r = self.client.post("/api/v1/sales/", {"lines": [{"product": self.p_a.pk, "quantity": "3"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Decimal(r.json()["total_amount"]), Decimal("600"))
        r = self.client.post("/api/v1/sales/", {"lines": [{"product": self.p_b.pk, "quantity": "1"}]}, format="json")
        self.assertEqual(r.status_code, 400)  # other business's product
        r = self.client.patch(f"/api/v1/products/{self.p_a.pk}/", {"selling_price": "1"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_insufficient_stock_returns_400(self):
        r = self.client.post("/api/v1/sales/", {"lines": [{"product": self.p_a.pk, "quantity": "99"}]}, format="json")
        self.assertEqual(r.status_code, 400)
