from django.contrib import admin

from .models import Category, InventoryTransaction, Product, StockBatch, StockCount, Supplier


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "business")
    list_filter = ("business",)
    search_fields = ("name",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "phone", "lead_time_days", "is_active")
    list_filter = ("business", "is_active")
    search_fields = ("name", "contact_person", "phone")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "business", "category", "stock_on_hand", "stock_risk", "is_active")
    list_filter = ("business", "category", "stock_risk", "is_active")
    search_fields = ("name", "sku", "barcode")
    readonly_fields = ("stock_on_hand", "forecast_daily_demand", "stock_risk", "intelligence_updated_at")


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    """Read-only: the ledger is append-only and changed only through services."""

    list_display = ("occurred_at", "product", "txn_type", "quantity", "balance_after", "reference", "created_by")
    list_filter = ("business", "txn_type")
    search_fields = ("product__name", "product__sku", "reference")
    date_hierarchy = "occurred_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ("product", "batch_number", "expiry_date", "quantity_received", "quantity_remaining")
    list_filter = ("business",)


@admin.register(StockCount)
class StockCountAdmin(admin.ModelAdmin):
    list_display = ("product", "counted_at", "system_quantity", "counted_quantity", "variance", "counted_by")
    list_filter = ("business",)
