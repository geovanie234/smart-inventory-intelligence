from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.urls import reverse

from apps.common.models import BusinessOwnedModel
from apps.inventory.models import MONEY, QTY, Product, Supplier


class PurchaseOrder(BusinessOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Awaiting approval"
        APPROVED = "approved", "Approved / ordered"
        PARTIAL = "partial", "Partially received"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.APPROVED, Status.PARTIAL)
    STATUS_BADGE = {
        "draft": "secondary", "submitted": "warning", "approved": "primary",
        "partial": "info", "received": "success", "cancelled": "dark",
    }

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    reference = models.CharField(max_length=30, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    first_received_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["business", "reference"], condition=~Q(reference=""), name="uniq_po_ref")
        ]

    def __str__(self):
        return self.reference or f"PO #{self.pk}"

    def get_absolute_url(self):
        return reverse("purchasing:po_detail", args=[self.pk])

    @property
    def total_cost(self) -> Decimal:
        return sum((i.line_total for i in self.items.all()), Decimal("0"))

    @property
    def status_badge(self):
        return self.STATUS_BADGE.get(self.status, "secondary")

    @property
    def is_editable(self):
        return self.status in (self.Status.DRAFT, self.Status.SUBMITTED)

    @property
    def is_receivable(self):
        return self.status in self.OPEN_STATUSES

    @property
    def actual_lead_time_days(self):
        if self.approved_at and self.first_received_at:
            return (self.first_received_at - self.approved_at).total_seconds() / 86400
        return None


class PurchaseItem(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="purchase_items")
    quantity_ordered = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    quantity_received = models.DecimalField(**QTY, default=Decimal("0"))
    unit_cost = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(quantity_ordered__gt=0), name="po_item_qty_positive"),
            models.CheckConstraint(condition=Q(quantity_received__gte=0), name="po_item_received_non_negative"),
            models.UniqueConstraint(fields=["order", "product"], name="uniq_product_per_po"),
        ]

    def __str__(self):
        return f"{self.quantity_ordered} × {self.product.name}"

    @property
    def line_total(self) -> Decimal:
        return self.quantity_ordered * self.unit_cost

    @property
    def outstanding(self) -> Decimal:
        return max(self.quantity_ordered - self.quantity_received, Decimal("0"))
