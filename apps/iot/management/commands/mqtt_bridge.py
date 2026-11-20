"""
MQTT → Django bridge.

Subscribes to `sis/<device_id>/readings` (configurable with MQTT_TOPIC) and feeds
each message through the same validation and ingestion path as the HTTPS API.

Payload (JSON):
    {"key": "<device key>", "message_id": "a1b2-17", "measured_at": "2026-11-20T08:15:00+02:00",
     "weight_g": 12850.5, "firmware": "1.2.0"}

Acknowledgements are published to `sis/<device_id>/ack` so firmware can drop the
message from its retry queue (QoS 1 + idempotent message ids = safe retries).
"""
import json
import logging
import signal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.iot.models import IoTReading
from apps.iot.services import authenticate_device, ingest_reading

logger = logging.getLogger("apps.iot.mqtt")


class Command(BaseCommand):
    help = "Run the MQTT bridge that ingests ESP32 sensor readings."

    def handle(self, *args, **opts):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise CommandError("paho-mqtt is not installed: pip install paho-mqtt") from exc

        cfg = settings.MQTT
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sis-django-bridge", clean_session=False)
        if cfg["USERNAME"]:
            client.username_pw_set(cfg["USERNAME"], cfg["PASSWORD"])
        if cfg["TLS"]:
            client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        def on_connect(c, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                self.stderr.write(f"MQTT connection failed: {reason_code}")
                return
            c.subscribe(cfg["TOPIC"], qos=1)
            self.stdout.write(self.style.SUCCESS(f"Connected to {cfg['HOST']}:{cfg['PORT']}, subscribed to {cfg['TOPIC']}"))

        def on_message(c, userdata, msg):
            close_old_connections()
            parts = msg.topic.split("/")
            device_id = parts[1] if len(parts) >= 3 else ""
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Discarding non-JSON message on %s", msg.topic)
                return
            device = authenticate_device(device_id, str(payload.get("key", "")))
            if device is None:
                logger.warning("Rejected message from unknown or unauthenticated device %r", device_id)
                return
            ack = {"message_id": payload.get("message_id")}
            try:
                result = ingest_reading(device, payload, channel=IoTReading.Channel.MQTT)
                ack.update(status="duplicate" if result.duplicate else "accepted", reading_status=result.reading.status)
            except ValidationError as exc:
                ack.update(status="rejected", errors=exc.messages)
                logger.warning("Rejected reading from %s: %s", device_id, exc.messages)
            c.publish(f"sis/{device_id}/ack", json.dumps(ack), qos=1)

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(cfg["HOST"], cfg["PORT"], keepalive=60)
        except OSError as exc:
            raise CommandError(f"Cannot reach MQTT broker at {cfg['HOST']}:{cfg['PORT']}: {exc}") from exc

        signal.signal(signal.SIGINT, lambda *a: client.disconnect())
        client.loop_forever(retry_first_connection=False)
