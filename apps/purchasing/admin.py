from django.contrib import admin

from .models import PurchaseItem, PurchaseOrder


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 0
    readonly_fields = ("quantity_received",)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("reference", "business", "supplier", "status", "expected_delivery_date", "created_at")
    list_filter = ("business", "status", "supplier")
    search_fields = ("reference", "supplier__name")
    inlines = [PurchaseItemInline]
    readonly_fields = ("approved_by", "approved_at", "first_received_at", "received_at", "submitted_at")
