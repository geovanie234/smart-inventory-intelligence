from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.common.utils import money
from apps.inventory.models import InventoryTransaction, Product
from apps.inventory.services import post_transaction

from .models import Sale, SaleItem

T = InventoryTransaction.Type


@dataclass
class SaleLine:
    product: Product
    quantity: Decimal
    unit_price: Decimal | None = None


@transaction.atomic
def record_sale(
    business, lines: list[SaleLine], *, user=None, sold_at=None, payment_method=Sale.PaymentMethod.CASH,
    customer_name: str = "", notes: str = "", reference: str = "", refresh_risk: bool = True,
) -> Sale:
    """Create a sale and its stock-out ledger entries atomically. Raises ValidationError on any failure."""
    if not lines:
        raise ValidationError("A sale needs at least one item.")
    # Merge duplicate scans of the same product at the same price into one line
    merged: dict[tuple, SaleLine] = {}
    for line in lines:
        if line.product.business_id != business.pk:
            raise ValidationError("Product does not belong to this business.")
        if line.quantity <= 0:
            raise ValidationError(f"Quantity for {line.product.name} must be positive.")
        key = (line.product.pk, line.unit_price)
        if key in merged:
            merged[key].quantity += line.quantity
        else:
            merged[key] = SaleLine(line.product, line.quantity, line.unit_price)

    sold_at = sold_at or timezone.now()
    sale = Sale.objects.create(
        business=business, cashier=user, sold_at=sold_at, payment_method=payment_method,
        customer_name=customer_name, notes=notes, reference=reference,
    )
    if not sale.reference:
        sale.reference = f"S-{timezone.localtime(sold_at):%y%m}-{sale.pk:06d}"
    total = Decimal("0")
    for line in merged.values():
        price = line.product.selling_price if line.unit_price is None else line.unit_price
        item = SaleItem.objects.create(
            sale=sale, product=line.product, quantity=line.quantity, unit_price=price,
            unit_cost=line.product.cost_price, line_total=money(price * line.quantity),
        )
        post_transaction(
            line.product, T.SALE, line.quantity, user=user, occurred_at=sold_at, reference=sale.reference,
            unit_cost=line.product.cost_price, sale_item=item, refresh_risk=refresh_risk,
        )
        total += item.line_total
    sale.total_amount = money(total)
    sale.save(update_fields=["total_amount", "reference"])
    return sale


@transaction.atomic
def void_sale(sale: Sale, *, user=None, reason: str = "") -> Sale:
    """Reverse a sale: stock is returned through SALE_RETURN entries; history is preserved."""
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    if sale.status == Sale.Status.VOIDED:
        raise ValidationError("This sale is already voided.")
    for item in sale.items.select_related("product"):
        post_transaction(
            item.product, T.SALE_RETURN, item.quantity, user=user, reference=f"VOID {sale.reference}",
            notes=reason or "Sale voided", unit_cost=item.unit_cost,
        )
    sale.status = Sale.Status.VOIDED
    sale.voided_at = timezone.now()
    sale.voided_by = user
    sale.notes = (sale.notes + f" | Voided: {reason}").strip(" |")[:255]
    sale.save(update_fields=["status", "voided_at", "voided_by", "notes"])
    return sale
