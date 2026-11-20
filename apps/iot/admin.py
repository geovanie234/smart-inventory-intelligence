from django.contrib import admin

from .models import IoTDevice, IoTReading


@admin.register(IoTDevice)
class IoTDeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "device_id", "business", "device_type", "product", "last_seen_at", "is_active")
    list_filter = ("business", "device_type", "is_active")
    readonly_fields = ("key_prefix", "last_seen_at")
    exclude = ("key_hash",)


@admin.register(IoTReading)
class IoTReadingAdmin(admin.ModelAdmin):
    list_display = ("device", "measured_at", "raw_value", "estimated_quantity", "recorded_quantity", "status", "channel")
    list_filter = ("business", "status", "channel")
    date_hierarchy = "measured_at"
