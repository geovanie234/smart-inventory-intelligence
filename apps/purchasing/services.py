from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.inventory.models import InventoryTransaction, Product, StockBatch
from apps.common.utils import fmt_qty
from apps.inventory.services import post_transaction, refresh_product_risk

from .models import PurchaseItem, PurchaseOrder

S = PurchaseOrder.Status


def on_order_quantity(product: Product) -> Decimal:
    """Quantity ordered from suppliers but not yet received (open purchase orders)."""
    agg = PurchaseItem.objects.filter(product=product, order__status__in=PurchaseOrder.OPEN_STATUSES).aggregate(
        ordered=Sum("quantity_ordered"), received=Sum("quantity_received")
    )
    return max((agg["ordered"] or Decimal("0")) - (agg["received"] or Decimal("0")), Decimal("0"))


def on_order_map(business) -> dict[int, Decimal]:
    rows = (
        PurchaseItem.objects.filter(order__business=business, order__status__in=PurchaseOrder.OPEN_STATUSES)
        .values("product_id")
        .annotate(o=Sum("quantity_ordered"), r=Sum("quantity_received"))
    )
    return {r["product_id"]: max(r["o"] - r["r"], Decimal("0")) for r in rows}


@transaction.atomic
def create_order(business, supplier, lines, *, user=None, notes="", expected_delivery_date=None) -> PurchaseOrder:
    """lines: iterable of (product, quantity, unit_cost)."""
    lines = [l for l in lines if l[1] and l[1] > 0]
    if not lines:
        raise ValidationError("A purchase order needs at least one item.")
    order = PurchaseOrder.objects.create(
        business=business, supplier=supplier, created_by=user, notes=notes,
        expected_delivery_date=expected_delivery_date,
    )
    order.reference = f"PO-{timezone.localdate():%y%m}-{order.pk:05d}"
    order.save(update_fields=["reference"])
    merged = defaultdict(lambda: [Decimal("0"), None])
    for product, qty, cost in lines:
        if product.business_id != business.pk:
            raise ValidationError("Product does not belong to this business.")
        merged[product][0] += Decimal(qty)
        merged[product][1] = Decimal(cost if cost is not None else product.cost_price)
    for product, (qty, cost) in merged.items():
        PurchaseItem.objects.create(order=order, product=product, quantity_ordered=qty, unit_cost=cost)
    return order


def submit_order(order: PurchaseOrder, user=None) -> PurchaseOrder:
    if order.status != S.DRAFT:
        raise ValidationError("Only draft orders can be submitted.")
    if not order.items.exists():
        raise ValidationError("Add at least one item before submitting.")
    order.status = S.SUBMITTED
    order.submitted_at = timezone.now()
    order.save(update_fields=["status", "submitted_at", "updated_at"])
    return order


def approve_order(order: PurchaseOrder, user) -> PurchaseOrder:
    if not user.is_manager:
        raise ValidationError("Only a manager can approve purchase orders.")
    if order.status not in (S.DRAFT, S.SUBMITTED):
        raise ValidationError("This order cannot be approved in its current state.")
    now = timezone.now()
    order.status = S.APPROVED
    order.approved_by = user
    order.approved_at = now
    if not order.expected_delivery_date:
        order.expected_delivery_date = (now + timedelta(days=order.supplier.lead_time_days)).date()
    order.save(update_fields=["status", "approved_by", "approved_at", "expected_delivery_date", "updated_at"])
    from apps.intelligence.models import ReorderRecommendation

    ReorderRecommendation.objects.filter(purchase_item__order=order).update(status=ReorderRecommendation.Status.ORDERED)
    _refresh_risks(order)
    return order


def cancel_order(order: PurchaseOrder, user=None) -> PurchaseOrder:
    if order.status in (S.RECEIVED, S.CANCELLED, S.PARTIAL):
        raise ValidationError("Received or partially received orders cannot be cancelled.")
    was_open = order.status in PurchaseOrder.OPEN_STATUSES
    order.status = S.CANCELLED
    order.save(update_fields=["status", "updated_at"])
    if was_open:
        _refresh_risks(order)
    return order


def _refresh_risks(order: PurchaseOrder) -> None:
    for item in order.items.select_related("product"):
        refresh_product_risk(item.product)


@transaction.atomic
def receive_items(order: PurchaseOrder, receipts: dict, *, user=None, received_at=None) -> list[InventoryTransaction]:
    """
    receipts: {purchase_item_id: {"quantity": Decimal, "expiry_date": date|None, "batch_number": str}}
    Posts PURCHASE ledger entries, creates expiry batches and advances the order status.
    """
    order = PurchaseOrder.objects.select_for_update().get(pk=order.pk)
    if not order.is_receivable:
        raise ValidationError("Only approved orders can be received.")
    received_at = received_at or timezone.now()
    txns = []
    items = {i.pk: i for i in order.items.select_related("product")}
    for item_id, data in receipts.items():
        item = items.get(int(item_id))
        qty = Decimal(data.get("quantity") or 0)
        if item is None or qty <= 0:
            continue
        if qty > item.outstanding * Decimal("1.10"):
            raise ValidationError(
                f"{item.product.name}: receiving {fmt_qty(qty)} exceeds the outstanding "
                f"{fmt_qty(item.outstanding)} by more than 10%."
            )
        txns.append(post_transaction(
            item.product, InventoryTransaction.Type.PURCHASE, qty, user=user, occurred_at=received_at,
            reference=order.reference, unit_cost=item.unit_cost, purchase_item=item,
        ))
        if item.product.track_expiry:
            if not data.get("expiry_date"):
                raise ValidationError(f"{item.product.name} tracks expiry: enter an expiry date.")
            StockBatch.objects.create(
                business=order.business, product=item.product, purchase_item=item,
                batch_number=data.get("batch_number", "")[:60], expiry_date=data["expiry_date"],
                quantity_received=qty, quantity_remaining=qty, received_at=received_at,
            )
        PurchaseItem.objects.filter(pk=item.pk).update(quantity_received=F("quantity_received") + qty)
        # Update cost price to latest purchase cost
        if item.unit_cost and item.product.cost_price != item.unit_cost:
            Product.objects.filter(pk=item.product_id).update(cost_price=item.unit_cost)
    if not txns:
        raise ValidationError("Enter a received quantity for at least one item.")

    order.first_received_at = order.first_received_at or received_at
    fully = all(i.outstanding <= 0 for i in order.items.all())
    order.status = S.RECEIVED if fully else S.PARTIAL
    if fully:
        order.received_at = received_at
    order.save(update_fields=["status", "first_received_at", "received_at", "updated_at"])
    for txn in txns:  # on-order quantity has changed now that receipts are booked
        refresh_product_risk(txn.product)
    return txns


def supplier_performance(supplier) -> dict:
    orders = list(
        PurchaseOrder.objects.filter(supplier=supplier, first_received_at__isnull=False, approved_at__isnull=False)
        .prefetch_related("items")
    )
    if not orders:
        return {"orders": 0}
    lead_times = [o.actual_lead_time_days for o in orders]
    on_time = [
        o for o in orders
        if o.expected_delivery_date and timezone.localtime(o.first_received_at).date() <= o.expected_delivery_date
    ]
    ordered = sum(i.quantity_ordered for o in orders for i in o.items.all())
    received = sum(i.quantity_received for o in orders for i in o.items.all())
    return {
        "orders": len(orders),
        "avg_lead_time": sum(lead_times) / len(lead_times),
        "on_time_rate": len(on_time) / len(orders) * 100,
        "fill_rate": float(received / ordered * 100) if ordered else None,
    }
