from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.common.models import BusinessOwnedModel

QTY = dict(max_digits=14, decimal_places=3)
MONEY = dict(max_digits=14, decimal_places=2)
ZERO = Decimal("0")


class Category(BusinessOwnedModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(fields=["business", "name"], name="uniq_category_per_business")]

    def __str__(self):
        return self.name


class Supplier(BusinessOwnedModel):
    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    lead_time_days = models.PositiveSmallIntegerField(
        default=3, help_text="Typical days between ordering and delivery."
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["business", "name"], name="uniq_supplier_per_business")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("inventory:supplier_detail", args=[self.pk])


class Product(BusinessOwnedModel):
    class Unit(models.TextChoices):
        PIECE = "pc", "Piece"
        KG = "kg", "Kilogram"
        LITRE = "l", "Litre"
        BOX = "box", "Box"
        PACK = "pack", "Pack"
        CRATE = "crate", "Crate"
        BAG = "bag", "Bag"
        BOTTLE = "bottle", "Bottle"

    class Risk(models.TextChoices):
        UNKNOWN = "unknown", "No forecast yet"
        OUT = "out", "Out of stock"
        CRITICAL = "critical", "Critical"
        LOW = "low", "Reorder now"
        OK = "ok", "Healthy"
        OVERSTOCK = "overstock", "Overstock"

    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="products")
    preferred_supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="products"
    )
    name = models.CharField(max_length=200)
    sku = models.CharField("SKU", max_length=60)
    barcode = models.CharField(max_length=64, blank=True, db_index=True)
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.PIECE)
    cost_price = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])
    selling_price = models.DecimalField(**MONEY, default=ZERO, validators=[MinValueValidator(ZERO)])

    # Replenishment parameters (blank = use supplier / business defaults or computed values)
    lead_time_days = models.PositiveSmallIntegerField(null=True, blank=True)
    safety_stock = models.DecimalField(
        **QTY, null=True, blank=True, validators=[MinValueValidator(ZERO)],
        help_text="Leave blank to compute from forecast error and service level.",
    )
    min_order_qty = models.DecimalField(**QTY, default=ZERO, validators=[MinValueValidator(ZERO)])
    pack_size = models.DecimalField(
        **QTY, default=Decimal("1"), validators=[MinValueValidator(Decimal("0.001"))],
        help_text="Orders are rounded up to multiples of this quantity.",
    )
    shelf_capacity = models.DecimalField(**QTY, null=True, blank=True, validators=[MinValueValidator(ZERO)])
    track_expiry = models.BooleanField(default=False)

    # Ledger-derived balance (kept in sync atomically by inventory.services)
    stock_on_hand = models.DecimalField(**QTY, default=ZERO, editable=False)

    # Intelligence snapshot (refreshed by forecasting runs and every stock movement)
    forecast_daily_demand = models.DecimalField(**QTY, null=True, blank=True, editable=False)
    stock_risk = models.CharField(max_length=12, choices=Risk.choices, default=Risk.UNKNOWN, editable=False)
    intelligence_updated_at = models.DateTimeField(null=True, blank=True, editable=False)

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["business", "sku"], name="uniq_sku_per_business"),
            models.UniqueConstraint(
                fields=["business", "barcode"], condition=~Q(barcode=""), name="uniq_barcode_per_business"
            ),
        ]
        indexes = [models.Index(fields=["business", "stock_risk"])]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    def get_absolute_url(self):
        return reverse("inventory:product_detail", args=[self.pk])

    # ------------------------------------------------------------------ #
    @property
    def effective_lead_time(self) -> int:
        if self.lead_time_days is not None:
            return self.lead_time_days
        if self.preferred_supplier_id:
            return self.preferred_supplier.lead_time_days
        return self.business.default_lead_time_days

    @property
    def stock_value(self) -> Decimal:
        return (self.stock_on_hand or ZERO) * (self.cost_price or ZERO)

    @property
    def margin_pct(self):
        if not self.selling_price:
            return None
        return (self.selling_price - self.cost_price) / self.selling_price * 100

    @property
    def days_of_cover(self):
        if self.forecast_daily_demand and self.forecast_daily_demand > 0:
            return float(self.stock_on_hand) / float(self.forecast_daily_demand)
        return None

    def on_order(self) -> Decimal:
        from apps.purchasing.services import on_order_quantity

        return on_order_quantity(self)

    def compute_risk(self, on_order=None) -> str:
        """
        Rule-based stock risk using the *inventory position* (on hand + already on order)
        against forecast demand over the supplier lead time and the review period.
        """
        if self.stock_on_hand <= 0:
            return self.Risk.OUT
        demand = float(self.forecast_daily_demand or 0)
        if demand <= 0:
            return self.Risk.UNKNOWN if self.forecast_daily_demand is None else self.Risk.OK
        on_order = self.on_order() if on_order is None else on_order
        position_cover = (float(self.stock_on_hand) + float(on_order)) / demand
        lead = self.effective_lead_time
        if position_cover < lead:
            return self.Risk.CRITICAL  # will run out before any new order can arrive
        if position_cover < lead + self.business.review_period_days:
            return self.Risk.LOW  # must be ordered at this review to last until the next delivery
        if float(self.stock_on_hand) / demand > self.business.overstock_cover_days:
            return self.Risk.OVERSTOCK
        return self.Risk.OK

    RISK_BADGE = {
        "unknown": "secondary", "out": "dark", "critical": "danger",
        "low": "warning", "ok": "success", "overstock": "info",
    }

    @property
    def risk_badge(self) -> str:
        return self.RISK_BADGE.get(self.stock_risk, "secondary")


class InventoryTransaction(BusinessOwnedModel):
    """Append-only stock ledger. On-hand stock = sum(quantity) per product."""

    class Type(models.TextChoices):
        OPENING = "opening", "Opening balance"
        PURCHASE = "purchase", "Goods received"
        SALE = "sale", "Sale"
        SALE_RETURN = "sale_return", "Customer return"
        SUPPLIER_RETURN = "supplier_return", "Return to supplier"
        ADJUSTMENT_IN = "adjustment_in", "Adjustment (in)"
        ADJUSTMENT_OUT = "adjustment_out", "Adjustment (out)"
        DAMAGE = "damage", "Damaged"
        LOSS = "loss", "Loss / shrinkage"
        EXPIRED = "expired", "Expired write-off"
        COUNT_CORRECTION = "count_correction", "Stock count correction"

    INBOUND = {Type.OPENING, Type.PURCHASE, Type.SALE_RETURN, Type.ADJUSTMENT_IN}
    OUTBOUND = {Type.SALE, Type.SUPPLIER_RETURN, Type.ADJUSTMENT_OUT, Type.DAMAGE, Type.LOSS, Type.EXPIRED}
    # Movement types reviewed by anomaly detection
    REVIEWABLE = {Type.ADJUSTMENT_OUT, Type.DAMAGE, Type.LOSS, Type.EXPIRED, Type.COUNT_CORRECTION}

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="transactions")
    txn_type = models.CharField("type", max_length=20, choices=Type.choices, db_index=True)
    quantity = models.DecimalField(**QTY, help_text="Signed: positive = stock in, negative = stock out.")
    balance_after = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY, null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    reference = models.CharField(max_length=80, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    sale_item = models.ForeignKey("sales.SaleItem", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    purchase_item = models.ForeignKey(
        "purchasing.PurchaseItem", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [models.CheckConstraint(condition=~Q(quantity=0), name="txn_quantity_not_zero")]
        indexes = [models.Index(fields=["business", "product", "occurred_at"])]

    def __str__(self):
        from apps.common.utils import fmt_qty

        return f"{self.get_txn_type_display()} {fmt_qty(abs(self.quantity))} {self.product.unit} × {self.product.name}"

    @property
    def is_inbound(self):
        return self.quantity > 0


class StockBatch(BusinessOwnedModel):
    """Received lot with an expiry date, depleted first-expiry-first-out (FEFO)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="batches")
    batch_number = models.CharField(max_length=60, blank=True)
    expiry_date = models.DateField(db_index=True)
    quantity_received = models.DecimalField(**QTY)
    quantity_remaining = models.DecimalField(**QTY)
    received_at = models.DateTimeField(default=timezone.now)
    purchase_item = models.ForeignKey(
        "purchasing.PurchaseItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="batches"
    )

    class Meta:
        ordering = ["expiry_date", "id"]
        verbose_name_plural = "stock batches"
        constraints = [
            models.CheckConstraint(condition=Q(quantity_remaining__gte=0), name="batch_remaining_non_negative")
        ]

    def __str__(self):
        return f"{self.product.name} batch {self.batch_number or self.pk} exp {self.expiry_date}"

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry_date - timezone.localdate()).days


class StockCount(BusinessOwnedModel):
    """Physical count of a product; any variance is posted to the ledger as a correction."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_counts")
    system_quantity = models.DecimalField(**QTY)
    counted_quantity = models.DecimalField(**QTY, validators=[MinValueValidator(ZERO)])
    variance = models.DecimalField(**QTY)
    counted_at = models.DateTimeField(default=timezone.now)
    counted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    transaction = models.OneToOneField(
        InventoryTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_count"
    )

    class Meta:
        ordering = ["-counted_at"]

    def __str__(self):
        return f"Count {self.product.name}: {self.counted_quantity} (variance {self.variance})"

    @property
    def variance_pct(self):
        if not self.system_quantity:
            return None
        return float(self.variance) / float(self.system_quantity) * 100
