from decimal import Decimal

from rest_framework import serializers

from apps.intelligence.models import Alert, DemandPrediction, ReorderRecommendation
from apps.inventory.models import Category, InventoryTransaction, Product, Supplier
from apps.sales.models import Sale, SaleItem


class BusinessScopedPKField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return super().get_queryset().filter(business=self.context["request"].business)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "description"]


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "name", "contact_person", "phone", "email", "address", "lead_time_days", "is_active"]


class ProductSerializer(serializers.ModelSerializer):
    category = BusinessScopedPKField(queryset=Category.objects.all(), allow_null=True, required=False)
    preferred_supplier = BusinessScopedPKField(queryset=Supplier.objects.all(), allow_null=True, required=False)
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    stock_risk_label = serializers.CharField(source="get_stock_risk_display", read_only=True)
    days_of_cover = serializers.FloatField(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "name", "sku", "barcode", "category", "category_name", "unit", "preferred_supplier",
            "cost_price", "selling_price", "lead_time_days", "safety_stock", "min_order_qty", "pack_size",
            "shelf_capacity", "track_expiry", "is_active",
            "stock_on_hand", "forecast_daily_demand", "days_of_cover", "stock_risk", "stock_risk_label",
        ]
        read_only_fields = ["stock_on_hand", "forecast_daily_demand", "stock_risk"]


class TransactionSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    type_label = serializers.CharField(source="get_txn_type_display", read_only=True)

    class Meta:
        model = InventoryTransaction
        fields = ["id", "product", "product_name", "txn_type", "type_label", "quantity", "balance_after",
                  "occurred_at", "reference", "notes"]


class SaleItemInputSerializer(serializers.Serializer):
    product = BusinessScopedPKField(queryset=Product.objects.filter(is_active=True))
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False)


class SaleItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = SaleItem
        fields = ["product", "product_name", "quantity", "unit_price", "line_total"]


class SaleSerializer(serializers.ModelSerializer):
    items = SaleItemSerializer(many=True, read_only=True)
    lines = SaleItemInputSerializer(many=True, write_only=True)

    class Meta:
        model = Sale
        fields = ["id", "reference", "sold_at", "payment_method", "customer_name", "total_amount", "status",
                  "notes", "items", "lines"]
        read_only_fields = ["reference", "total_amount", "status"]


class PredictionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DemandPrediction
        fields = ["product", "forecast_date", "predicted_quantity", "lower_bound", "upper_bound", "model_name", "run"]


class RecommendationSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = ReorderRecommendation
        fields = ["id", "product", "product_name", "supplier", "status", "risk_level", "current_stock", "on_order",
                  "avg_daily_demand", "lead_time_days", "review_period_days", "safety_stock", "expected_demand",
                  "recommended_quantity", "days_of_cover", "estimated_cost", "explanation", "created_at"]


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ["id", "alert_type", "severity", "status", "title", "message", "product", "score",
                  "feedback", "created_at", "resolved_at"]
        read_only_fields = [f for f in fields if f not in ("status", "feedback")]
