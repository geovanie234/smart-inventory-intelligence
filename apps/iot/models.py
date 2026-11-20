import hashlib
import hmac
import secrets
from datetime import timedelta
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.common.models import BusinessOwnedModel
from apps.inventory.models import QTY, Product


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class IoTDevice(BusinessOwnedModel):
    class DeviceType(models.TextChoices):
        LOAD_CELL = "load_cell", "ESP32 + load cell (weight)"
        BARCODE = "barcode", "Barcode scanner gateway"
        RFID = "rfid", "RFID reader"

    name = models.CharField(max_length=100)
    device_id = models.SlugField(max_length=64, unique=True, help_text="Unique identifier flashed onto the device.")
    device_type = models.CharField(max_length=12, choices=DeviceType.choices, default=DeviceType.LOAD_CELL)
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="devices",
        help_text="Product monitored by this sensor (load cells).",
    )
    location = models.CharField(max_length=120, blank=True, help_text="e.g. Store room, shelf B2")
    key_prefix = models.CharField(max_length=12, editable=False, blank=True)
    key_hash = models.CharField(max_length=64, editable=False, blank=True)

    # Load-cell calibration
    unit_weight_grams = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Net weight of one stock unit, used to convert weight to quantity.",
    )
    tare_grams = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0"),
        help_text="Weight of the empty container/platform.",
    )
    tolerance_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("5"),
        help_text="Differences within this % of recorded stock are treated as normal measurement noise.",
    )
    expected_interval_seconds = models.PositiveIntegerField(default=300)
    firmware_version = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.device_id})"

    def get_absolute_url(self):
        return reverse("iot:device_detail", args=[self.pk])

    def rotate_key(self) -> str:
        """Generate a new API key. The raw key is returned once and only its hash is stored."""
        raw = "sisd_" + secrets.token_urlsafe(32)
        self.key_prefix = raw[:12]
        self.key_hash = _hash_key(raw)
        return raw

    def check_key(self, raw: str) -> bool:
        return bool(self.key_hash) and hmac.compare_digest(self.key_hash, _hash_key(raw or ""))

    @property
    def is_online(self) -> bool:
        if not self.last_seen_at:
            return False
        return timezone.now() - self.last_seen_at <= timedelta(seconds=self.expected_interval_seconds * 3)


class IoTReading(BusinessOwnedModel):
    class Status(models.TextChoices):
        OK = "ok", "Matches records"
        DISCREPANCY = "discrepancy", "Discrepancy"
        VERIFIED = "verified", "Verified — stock corrected"
        DISMISSED = "dismissed", "Dismissed"
        INVALID = "invalid", "Invalid reading"
        EVENT = "event", "Scan event"

    class Channel(models.TextChoices):
        HTTP = "http", "HTTPS"
        MQTT = "mqtt", "MQTT"

    STATUS_BADGE = {"ok": "success", "discrepancy": "danger", "verified": "primary",
                    "dismissed": "secondary", "invalid": "dark", "event": "info"}

    device = models.ForeignKey(IoTDevice, on_delete=models.CASCADE, related_name="readings")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="iot_readings")
    message_id = models.CharField(max_length=64, help_text="Device-generated id used to drop duplicate deliveries.")
    channel = models.CharField(max_length=5, choices=Channel.choices, default=Channel.HTTP)
    measured_at = models.DateTimeField(db_index=True)
    received_at = models.DateTimeField(auto_now_add=True)
    raw_value = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    estimated_quantity = models.DecimalField(**QTY, null=True, blank=True)
    recorded_quantity = models.DecimalField(**QTY, null=True, blank=True)
    discrepancy = models.DecimalField(**QTY, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OK, db_index=True)
    note = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-measured_at"]
        constraints = [models.UniqueConstraint(fields=["device", "message_id"], name="uniq_reading_per_device_msg")]

    def __str__(self):
        return f"{self.device.device_id} @ {self.measured_at:%Y-%m-%d %H:%M}"

    @property
    def status_badge(self):
        return self.STATUS_BADGE.get(self.status, "secondary")
