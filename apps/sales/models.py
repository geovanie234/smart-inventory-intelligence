from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.common.models import BusinessOwnedModel
from apps.inventory.models import MONEY, QTY, Product


class Sale(BusinessOwnedModel):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        MOBILE_MONEY = "momo", "Mobile money"
        CARD = "card", "Card"
        CREDIT = "credit", "Customer credit"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        VOIDED = "voided", "Voided"

    reference = models.CharField(max_length=30, blank=True)
    sold_at = models.DateTimeField(default=timezone.now, db_index=True)
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    customer_name = models.CharField(max_length=120, blank=True, help_text="Optional. Avoid storing unnecessary personal data.")
    payment_method = models.CharField(max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    total_amount = models.DecimalField(**MONEY, default=Decimal("0"))
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED, db_index=True)
    notes = models.CharField(max_length=255, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-sold_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["business", "reference"], condition=~Q(reference=""), name="uniq_sale_ref")
        ]
        indexes = [models.Index(fields=["business", "sold_at"])]

    def __str__(self):
        return self.reference or f"Sale #{self.pk}"

    def get_absolute_url(self):
        return reverse("sales:sale_detail", args=[self.pk])

    @property
    def item_count(self):
        return self.items.count()


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_items")
    quantity = models.DecimalField(**QTY, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(**MONEY, validators=[MinValueValidator(Decimal("0"))])
    unit_cost = models.DecimalField(**MONEY, default=Decimal("0"), help_text="Cost snapshot for margin reporting.")
    line_total = models.DecimalField(**MONEY)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="sale_item_qty_positive")]

    def __str__(self):
        return f"{self.quantity} × {self.product.name}"

    @property
    def margin(self):
        return self.line_total - self.unit_cost * self.quantity
