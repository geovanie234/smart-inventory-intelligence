from django.contrib import admin

from .models import Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    readonly_fields = ("product", "quantity", "unit_price", "unit_cost", "line_total")
    can_delete = False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("reference", "business", "sold_at", "total_amount", "payment_method", "status", "cashier")
    list_filter = ("business", "status", "payment_method")
    search_fields = ("reference", "customer_name")
    date_hierarchy = "sold_at"
    inlines = [SaleItemInline]
    readonly_fields = ("total_amount",)

    def has_delete_permission(self, request, obj=None):
        return False  # void through the application to keep the ledger consistent
