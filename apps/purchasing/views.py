from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.models import User
from apps.common.mixins import BusinessScopedQuerysetMixin, PageTitleMixin, RoleRequiredMixin
from apps.inventory.models import Product, Supplier

from . import services
from .models import PurchaseOrder

R = User.Role


class PurchaseOrderListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = PurchaseOrder
    template_name = "purchasing/po_list.html"
    context_object_name = "orders"
    paginate_by = 30
    page_title = "Purchase orders"
    page_subtitle = "Draft, approval, ordering and receiving"

    def get_queryset(self):
        qs = super().get_queryset().select_related("supplier", "created_by", "approved_by").annotate(n_items=Count("items")).order_by("-created_at")
        if status := self.request.GET.get("status"):
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = PurchaseOrder.Status.choices
        ctx["status_counts"] = dict(
            PurchaseOrder.objects.filter(business=self.request.business).values_list("status").annotate(n=Count("id"))
        )
        return ctx


class PurchaseOrderDetailView(PageTitleMixin, BusinessScopedQuerysetMixin, DetailView):
    model = PurchaseOrder
    template_name = "purchasing/po_detail.html"
    context_object_name = "order"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "created_by", "approved_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Purchase order {self.object.reference}"
        ctx["page_subtitle"] = self.object.supplier.name
        ctx["items"] = self.object.items.select_related("product")
        ctx["recommendations"] = self.object.items.filter(recommendations__isnull=False).count()
        return ctx


class PurchaseOrderCreateView(RoleRequiredMixin, View):
    allowed_roles = (R.PURCHASING,)
    template_name = "purchasing/po_form.html"

    def _ctx(self, request):
        return {
            "page_title": "New purchase order",
            "page_subtitle": "Orders are approved by a manager before being sent to the supplier",
            "suppliers": Supplier.objects.filter(business=request.business, is_active=True),
            "products": Product.objects.filter(business=request.business, is_active=True).select_related("preferred_supplier"),
        }

    def get(self, request):
        return render(request, self.template_name, self._ctx(request))

    def post(self, request):
        supplier = Supplier.objects.filter(business=request.business, pk=request.POST.get("supplier") or 0).first()
        products = {str(p.pk): p for p in Product.objects.filter(business=request.business)}
        lines, errors = [], []
        for pid, qty, cost in zip(request.POST.getlist("product"), request.POST.getlist("quantity"),
                                  request.POST.getlist("unit_cost")):
            if not pid:
                continue
            try:
                lines.append((products[pid], Decimal(qty), Decimal(cost) if cost else None))
            except (KeyError, InvalidOperation):
                errors.append("Check product, quantity and cost on every line.")
        if supplier is None:
            errors.append("Select a supplier.")
        if not errors:
            try:
                order = services.create_order(
                    request.business, supplier, lines, user=request.user, notes=request.POST.get("notes", ""),
                    expected_delivery_date=parse_date(request.POST.get("expected_delivery_date") or ""),
                )
                messages.success(request, f"{order.reference} created as draft.")
                return redirect(order.get_absolute_url())
            except ValidationError as exc:
                errors.extend(exc.messages)
        for e in errors:
            messages.error(request, e)
        return render(request, self.template_name, self._ctx(request), status=400)


class PurchaseOrderActionView(RoleRequiredMixin, View):
    """Submit / approve / cancel transitions."""

    allowed_roles = (R.PURCHASING,)

    def post(self, request, pk, action):
        order = get_object_or_404(PurchaseOrder, pk=pk, business=request.business)
        try:
            if action == "submit":
                services.submit_order(order, request.user)
                messages.success(request, f"{order.reference} submitted for manager approval.")
            elif action == "approve":
                services.approve_order(order, request.user)
                messages.success(request, f"{order.reference} approved. Expected delivery {order.expected_delivery_date:%d %b %Y}.")
            elif action == "cancel":
                services.cancel_order(order, request.user)
                messages.info(request, f"{order.reference} cancelled.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect(order.get_absolute_url())


class PurchaseOrderReceiveView(RoleRequiredMixin, View):
    allowed_roles = (R.CLERK, R.PURCHASING)
    template_name = "purchasing/po_receive.html"

    def get(self, request, pk):
        order = get_object_or_404(PurchaseOrder, pk=pk, business=request.business)
        return render(request, self.template_name, {
            "page_title": f"Receive {order.reference}",
            "page_subtitle": order.supplier.name,
            "order": order,
            "items": order.items.select_related("product"),
        })

    def post(self, request, pk):
        order = get_object_or_404(PurchaseOrder, pk=pk, business=request.business)
        receipts = {}
        for item in order.items.all():
            raw = request.POST.get(f"qty_{item.pk}", "").strip()
            if not raw:
                continue
            try:
                qty = Decimal(raw)
            except InvalidOperation:
                messages.error(request, "Received quantities must be numbers.")
                return redirect("purchasing:po_receive", pk=pk)
            receipts[item.pk] = {
                "quantity": qty,
                "expiry_date": parse_date(request.POST.get(f"expiry_{item.pk}") or ""),
                "batch_number": request.POST.get(f"batch_{item.pk}", ""),
            }
        try:
            txns = services.receive_items(order, receipts, user=request.user)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("purchasing:po_receive", pk=pk)
        messages.success(request, f"Received {len(txns)} line(s) into stock.")
        return redirect(order.get_absolute_url())
