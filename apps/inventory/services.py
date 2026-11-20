"""
Stock ledger services.

All stock movements go through `post_transaction`, which locks the product row,
validates the movement, appends an immutable ledger entry and updates the cached
balance inside one database transaction. This protects stock figures from
duplicate or partial updates (proposal §9).
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import InventoryTransaction, Product, StockBatch, StockCount
from apps.common.utils import fmt_qty

T = InventoryTransaction.Type


class InsufficientStockError(ValidationError):
    pass


def _as_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@transaction.atomic
def post_transaction(
    product: Product,
    txn_type: str,
    quantity,
    *,
    user=None,
    occurred_at=None,
    reference: str = "",
    notes: str = "",
    unit_cost=None,
    allow_negative: bool = False,
    refresh_risk: bool = True,
    **links,
) -> InventoryTransaction:
    """
    Append a ledger entry. `quantity` is the absolute amount for typed movements
    (sign derived from the type) or a signed amount for count corrections.
    """
    qty = _as_decimal(quantity)
    if txn_type in InventoryTransaction.INBOUND:
        signed = abs(qty)
    elif txn_type in InventoryTransaction.OUTBOUND:
        signed = -abs(qty)
    elif txn_type == T.COUNT_CORRECTION:
        signed = qty
    else:
        raise ValidationError(f"Unknown transaction type: {txn_type}")
    if signed == 0:
        raise ValidationError("Quantity must not be zero.")

    locked = Product.objects.select_for_update().select_related("business", "preferred_supplier").get(pk=product.pk)
    new_balance = locked.stock_on_hand + signed
    if new_balance < 0 and not allow_negative:
        raise InsufficientStockError(
            f"Insufficient stock for {locked.name}: {fmt_qty(locked.stock_on_hand)} on hand, "
            f"{fmt_qty(abs(signed))} requested."
        )

    txn = InventoryTransaction.objects.create(
        business_id=locked.business_id,
        product=locked,
        txn_type=txn_type,
        quantity=signed,
        balance_after=new_balance,
        unit_cost=unit_cost,
        occurred_at=occurred_at or timezone.now(),
        reference=reference[:80],
        notes=notes[:255],
        created_by=user,
        **links,
    )

    locked.stock_on_hand = new_balance
    update_fields = ["stock_on_hand", "updated_at"]
    previous_risk = locked.stock_risk
    if refresh_risk:
        locked.stock_risk = locked.compute_risk()
        update_fields.append("stock_risk")
    locked.save(update_fields=update_fields)
    if refresh_risk and locked.stock_risk != previous_risk:
        from apps.intelligence.services.alerts import sync_product_stock_alert

        sync_product_stock_alert(locked)

    if signed < 0 and locked.track_expiry:
        _consume_batches(locked, abs(signed))

    # keep caller's instance current
    product.stock_on_hand = locked.stock_on_hand
    product.stock_risk = locked.stock_risk
    return txn


def _consume_batches(product: Product, quantity: Decimal) -> None:
    """Deplete batches first-expiry-first-out."""
    remaining = quantity
    for batch in StockBatch.objects.select_for_update().filter(product=product, quantity_remaining__gt=0):
        if remaining <= 0:
            break
        take = min(batch.quantity_remaining, remaining)
        batch.quantity_remaining -= take
        batch.save(update_fields=["quantity_remaining", "updated_at"])
        remaining -= take


@transaction.atomic
def record_stock_count(product: Product, counted_quantity, *, user=None, notes: str = "", counted_at=None) -> StockCount:
    counted = _as_decimal(counted_quantity)
    if counted < 0:
        raise ValidationError("Counted quantity cannot be negative.")
    locked = Product.objects.select_for_update().get(pk=product.pk)
    variance = counted - locked.stock_on_hand
    count = StockCount.objects.create(
        business_id=locked.business_id,
        product=locked,
        system_quantity=locked.stock_on_hand,
        counted_quantity=counted,
        variance=variance,
        counted_by=user,
        notes=notes,
        counted_at=counted_at or timezone.now(),
    )
    if variance != 0:
        count.transaction = post_transaction(
            locked, T.COUNT_CORRECTION, variance, user=user, occurred_at=count.counted_at,
            reference=f"COUNT-{count.pk}", notes=notes or "Physical count variance", allow_negative=True,
        )
        count.save(update_fields=["transaction"])
    return count


def refresh_product_risk(product: Product) -> str:
    """Recompute risk after events that change the inventory position without a stock movement
    (purchase order approved/cancelled, receipt completed, forecast updated)."""
    product = Product.objects.select_related("business", "preferred_supplier").get(pk=product.pk)
    new = product.compute_risk()
    if new != product.stock_risk:
        product.stock_risk = new
        product.save(update_fields=["stock_risk"])
        from apps.intelligence.services.alerts import sync_product_stock_alert

        sync_product_stock_alert(product)
    return new


def ledger_balance(product: Product) -> Decimal:
    return product.transactions.aggregate(total=Sum("quantity"))["total"] or Decimal("0")


@transaction.atomic
def reconcile_product(product: Product) -> tuple[Decimal, Decimal]:
    """Recompute the cached balance from the ledger. Returns (previous, ledger)."""
    locked = Product.objects.select_for_update().get(pk=product.pk)
    ledger = ledger_balance(locked)
    previous = locked.stock_on_hand
    if previous != ledger:
        locked.stock_on_hand = ledger
        locked.stock_risk = locked.compute_risk()
        locked.save(update_fields=["stock_on_hand", "stock_risk", "updated_at"])
    return previous, ledger
