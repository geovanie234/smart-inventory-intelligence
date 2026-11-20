"""
IoT ingestion (proposal §7 data flow):

1. device sends a reading (HTTPS or MQTT) with its device id, key and a message id;
2. the backend validates the device and the message;
3. the reading is stored, converted to an estimated quantity and compared with the ledger;
4. discrepancies raise an alert for a staff member to verify.

A sensor reading NEVER overwrites inventory automatically. Staff may confirm a
reading, which posts an auditable stock-count correction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.intelligence.models import Alert
from apps.intelligence.services.alerts import auto_resolve, raise_alert
from apps.inventory.models import Product
from apps.common.utils import fmt_qty
from apps.inventory.services import record_stock_count

from .models import IoTDevice, IoTReading

MAX_CLOCK_SKEW = timedelta(minutes=10)
MAX_BACKFILL = timedelta(days=7)


@dataclass
class IngestResult:
    reading: IoTReading
    duplicate: bool = False


def authenticate_device(device_id: str, key: str) -> IoTDevice | None:
    device = IoTDevice.objects.select_related("business", "product").filter(device_id=device_id, is_active=True).first()
    if device and device.business.is_active and device.check_key(key):
        return device
    return None


def _parse_measured_at(value) -> timezone.datetime:
    now = timezone.now()
    if not value:
        return now
    dt = parse_datetime(str(value))
    if dt is None:
        raise ValidationError("measured_at must be an ISO-8601 timestamp.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    if dt > now + MAX_CLOCK_SKEW:
        raise ValidationError("measured_at is in the future; check the device clock (NTP).")
    if dt < now - MAX_BACKFILL:
        raise ValidationError("measured_at is older than the 7-day backfill window.")
    return dt


def ingest_reading(device: IoTDevice, payload: dict, channel: str = IoTReading.Channel.HTTP) -> IngestResult:
    if not isinstance(payload, dict):
        raise ValidationError("Payload must be a JSON object.")
    message_id = str(payload.get("message_id") or "").strip()[:64]
    if not message_id:
        raise ValidationError("message_id is required (used to discard duplicate deliveries).")

    existing = IoTReading.objects.filter(device=device, message_id=message_id).first()
    if existing:
        return IngestResult(existing, duplicate=True)

    measured_at = _parse_measured_at(payload.get("measured_at"))
    safe_payload = {k: v for k, v in payload.items() if k not in {"key", "device_key"}}
    reading = IoTReading(
        business=device.business, device=device, message_id=message_id, channel=channel,
        measured_at=measured_at, payload=safe_payload,
    )

    if device.device_type == IoTDevice.DeviceType.LOAD_CELL:
        _process_weight(device, reading, payload)
    else:
        _process_scan(device, reading, payload)

    try:
        with transaction.atomic():
            reading.save()
    except IntegrityError:  # concurrent duplicate delivery
        return IngestResult(IoTReading.objects.get(device=device, message_id=message_id), duplicate=True)

    IoTDevice.objects.filter(pk=device.pk).update(
        last_seen_at=timezone.now(), firmware_version=str(payload.get("firmware", device.firmware_version))[:30]
    )
    _update_alerts(device, reading)
    return IngestResult(reading)


def _process_weight(device: IoTDevice, reading: IoTReading, payload: dict) -> None:
    try:
        weight = Decimal(str(payload.get("weight_g")))
    except (InvalidOperation, TypeError):
        raise ValidationError("weight_g (grams) is required for load-cell devices.")
    reading.raw_value = weight
    reading.product = device.product
    if device.product is None or not device.unit_weight_grams:
        reading.status = IoTReading.Status.INVALID
        reading.note = "Device not calibrated: link a product and set the unit weight."
        return
    net = weight - device.tare_grams
    if net < -device.unit_weight_grams or weight > Decimal("10000000"):
        reading.status = IoTReading.Status.INVALID
        reading.note = "Weight outside plausible range (check calibration or objects on the scale)."
        return
    estimated = max(net / device.unit_weight_grams, Decimal("0")).quantize(Decimal("0.001"))
    recorded = Product.objects.values_list("stock_on_hand", flat=True).get(pk=device.product_id)
    discrepancy = estimated - recorded
    tolerance = max(recorded * device.tolerance_pct / 100, Decimal("1"))
    reading.estimated_quantity = estimated
    reading.recorded_quantity = recorded
    reading.discrepancy = discrepancy
    reading.status = IoTReading.Status.DISCREPANCY if abs(discrepancy) > tolerance else IoTReading.Status.OK


def _process_scan(device: IoTDevice, reading: IoTReading, payload: dict) -> None:
    code = str(payload.get("barcode") or payload.get("tag") or "").strip()
    if not code:
        raise ValidationError("barcode or tag is required for scanner devices.")
    reading.product = Product.objects.filter(business=device.business).filter(Q(barcode=code) | Q(sku__iexact=code)).first()
    reading.status = IoTReading.Status.EVENT if reading.product else IoTReading.Status.INVALID
    reading.note = "" if reading.product else f"Unknown code {code}"
    try:
        reading.raw_value = Decimal(str(payload.get("quantity", 1)))
    except InvalidOperation:
        reading.raw_value = Decimal("1")


def _update_alerts(device: IoTDevice, reading: IoTReading) -> None:
    key = f"iot:{device.pk}"
    if reading.status == IoTReading.Status.DISCREPANCY:
        p = reading.product
        unit = p.unit
        raise_alert(
            device.business, Alert.Type.IOT_DISCREPANCY, key,
            f"Sensor disagrees with records: {p.name}",
            f"{device.name} estimates {fmt_qty(reading.estimated_quantity)} {unit} "
            f"({fmt_qty(reading.raw_value)} g) but the ledger shows {fmt_qty(reading.recorded_quantity)} "
            f"({reading.discrepancy:+.1f}). Check the shelf: it may be unrecorded sales/receipts, misplaced items, "
            "packaging or calibration. Verify the reading on the device page to correct stock.",
            Alert.Severity.WARNING, product=p, iot_reading=reading, score=float(reading.discrepancy),
        )
    elif reading.status == IoTReading.Status.OK:
        Alert.objects.filter(
            business=device.business, dedupe_key=key, status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED]
        ).update(status=Alert.Status.RESOLVED, resolved_at=timezone.now(),
                 resolution_note="Later sensor reading matches records.")


@transaction.atomic
def verify_reading(reading: IoTReading, user) -> None:
    """Staff confirmed the physical quantity: post a count correction to the ledger."""
    if reading.status != IoTReading.Status.DISCREPANCY:
        raise ValidationError("Only discrepancy readings can be verified.")
    record_stock_count(
        reading.product, reading.estimated_quantity, user=user,
        notes=f"Verified sensor reading {reading.device.device_id} #{reading.pk}",
    )
    reading.status = IoTReading.Status.VERIFIED
    reading.save(update_fields=["status"])
    Alert.objects.filter(iot_reading=reading).update(
        status=Alert.Status.RESOLVED, resolved_at=timezone.now(), resolved_by=user,
        resolution_note="Reading verified and stock corrected.", feedback=Alert.Feedback.USEFUL,
    )


def dismiss_reading(reading: IoTReading, user, note: str = "") -> None:
    reading.status = IoTReading.Status.DISMISSED
    reading.note = note[:255] or "Dismissed by staff"
    reading.save(update_fields=["status", "note"])
    Alert.objects.filter(iot_reading=reading).update(
        status=Alert.Status.RESOLVED, resolved_at=timezone.now(), resolved_by=user,
        resolution_note="Reading dismissed.", feedback=Alert.Feedback.FALSE_ALARM,
    )


def check_device_health(business) -> int:
    keys, raised = [], 0
    now = timezone.now()
    for d in IoTDevice.objects.filter(business=business, is_active=True):
        if d.is_online:
            continue
        key = f"offline:{d.pk}"
        since = f"since {timezone.localtime(d.last_seen_at):%d %b %H:%M}" if d.last_seen_at else "— never connected"
        _, created = raise_alert(
            business, Alert.Type.DEVICE_OFFLINE, key, f"{d.name} is offline",
            f"No readings received {since}. Expected every {d.expected_interval_seconds // 60} min. "
            "Check power, Wi-Fi and the broker connection.",
            Alert.Severity.INFO,
        )
        raised += created
        keys.append(key)
    auto_resolve(business, (Alert.Type.DEVICE_OFFLINE,), keys, note="Device reconnected.")
    return raised
