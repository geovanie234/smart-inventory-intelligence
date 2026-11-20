import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.models import User
from apps.common.mixins import BusinessRequiredMixin, BusinessScopedQuerysetMixin, PageTitleMixin, RoleRequiredMixin
from apps.common.utils import csv_response, fmt_qty

from . import services
from .forms import (
    CategoryForm, LedgerFilterForm, ProductFilterForm, ProductForm, StockAdjustmentForm, SupplierForm,
)
from .models import Category, InventoryTransaction, Product, StockBatch, StockCount, Supplier

R = User.Role
CATALOG_ROLES = (R.PURCHASING,)
STOCK_ROLES = (R.CLERK,)


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
class ProductListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = Product
    template_name = "inventory/product_list.html"
    context_object_name = "products"
    paginate_by = 25
    page_title = "Products"
    page_subtitle = "Catalogue, live stock levels and AI risk status"

    def get_queryset(self):
        qs = super().get_queryset().select_related("category", "preferred_supplier", "business")
        self.filter_form = ProductFilterForm(self.request.GET or None, business=self.request.business)
        if self.filter_form.is_valid():
            d = self.filter_form.cleaned_data
            if d.get("q"):
                qs = qs.filter(Q(name__icontains=d["q"]) | Q(sku__icontains=d["q"]) | Q(barcode=d["q"]))
            if d.get("category"):
                qs = qs.filter(category=d["category"])
            if d.get("risk"):
                qs = qs.filter(stock_risk=d["risk"])
        if self.request.GET.get("inactive") != "1":
            qs = qs.filter(is_active=True)
        return qs

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            rows = (
                [p.sku, p.name, p.category or "", p.get_unit_display(), p.stock_on_hand, p.cost_price,
                 p.selling_price, p.stock_value, p.forecast_daily_demand or "", p.get_stock_risk_display()]
                for p in self.get_queryset()
            )
            return csv_response(
                "products.csv",
                ["SKU", "Name", "Category", "Unit", "On hand", "Cost", "Price", "Stock value",
                 "Forecast daily demand", "Status"],
                rows,
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        base = Product.objects.filter(business=self.request.business, is_active=True)
        ctx["risk_counts"] = dict(base.values_list("stock_risk").annotate(n=Count("id")))
        return ctx


class ProductDetailView(PageTitleMixin, BusinessScopedQuerysetMixin, DetailView):
    model = Product
    template_name = "inventory/product_detail.html"
    context_object_name = "product"

    def get_context_data(self, **kwargs):
        from apps.intelligence.models import Alert, DemandPrediction, ModelEvaluation, ReorderRecommendation
        from apps.sales.models import SaleItem

        ctx = super().get_context_data(**kwargs)
        p = self.object
        ctx["page_title"] = p.name
        ctx["page_subtitle"] = f"SKU {p.sku}" + (f" · {p.category}" if p.category else "")

        today = timezone.localdate()
        start = today - timedelta(days=90)  # 90 complete days, ending yesterday
        daily = dict(
            SaleItem.objects.filter(product=p, sale__status="completed", sale__sold_at__date__gte=start,
                                    sale__sold_at__date__lt=today)
            .annotate(day=TruncDate("sale__sold_at"))
            .values_list("day")
            .annotate(q=Sum("quantity"))
        )
        history = [
            {"x": (start + timedelta(days=i)).isoformat(), "y": float(daily.get(start + timedelta(days=i), 0))}
            for i in range(90)
        ]
        latest_pred = DemandPrediction.objects.filter(product=p).order_by("-run_id").first()
        forecast = []
        if latest_pred:
            preds = DemandPrediction.objects.filter(product=p, run_id=latest_pred.run_id).order_by("forecast_date")
            forecast = [
                {"x": d.forecast_date.isoformat(), "y": float(d.predicted_quantity),
                 "lo": float(d.lower_bound), "hi": float(d.upper_bound)}
                for d in preds
            ]
            ctx["forecast_model"] = latest_pred.model_name
            ctx["evaluations"] = ModelEvaluation.objects.filter(run_id=latest_pred.run_id, product=p).order_by("wape")
        ctx["chart_data"] = {"history": history, "forecast": forecast}
        ctx["transactions"] = p.transactions.select_related("created_by")[:15]
        ctx["batches"] = p.batches.filter(quantity_remaining__gt=0)[:10]
        ctx["counts"] = p.stock_counts.select_related("counted_by")[:5]
        ctx["recommendation"] = ReorderRecommendation.objects.filter(product=p).order_by("-created_at").first()
        ctx["alerts"] = Alert.objects.filter(product=p, status=Alert.Status.OPEN)[:5]
        ctx["sold_30d"] = sum(h["y"] for h in history[-30:])
        return ctx


class ProductCreateView(PageTitleMixin, RoleRequiredMixin, BusinessScopedQuerysetMixin, SuccessMessageMixin, CreateView):
    allowed_roles = CATALOG_ROLES
    model = Product
    form_class = ProductForm
    template_name = "common/form.html"
    page_title = "New product"
    success_message = "Product created."

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        opening = form.cleaned_data.get("opening_stock")
        if opening:
            services.post_transaction(
                self.object, InventoryTransaction.Type.OPENING, opening,
                user=self.request.user, unit_cost=self.object.cost_price, notes="Opening balance",
            )
        _warn_negative_margin(self.request, self.object)
        return response


class ProductUpdateView(PageTitleMixin, RoleRequiredMixin, BusinessScopedQuerysetMixin, SuccessMessageMixin, UpdateView):
    allowed_roles = CATALOG_ROLES
    model = Product
    form_class = ProductForm
    template_name = "common/form.html"
    page_title = "Edit product"
    success_message = "Product updated."

    def form_valid(self, form):
        response = super().form_valid(form)
        self.object.stock_risk = self.object.compute_risk()
        self.object.save(update_fields=["stock_risk"])
        _warn_negative_margin(self.request, self.object)
        return response


def _warn_negative_margin(request, product):
    if product.selling_price and product.selling_price < product.cost_price:
        messages.warning(request, f"{product.name} is priced below cost.")


class ProductLookupView(BusinessRequiredMixin, View):
    """Barcode / SKU lookup used by scanner workflows (sales, receiving, counts)."""

    def get(self, request):
        code = (request.GET.get("code") or "").strip()
        if not code:
            return JsonResponse({"found": False}, status=400)
        p = Product.objects.filter(business=request.business, is_active=True).filter(
            Q(barcode=code) | Q(sku__iexact=code)
        ).first()
        if not p:
            return JsonResponse({"found": False, "code": code}, status=404)
        return JsonResponse({
            "found": True, "id": p.pk, "name": p.name, "sku": p.sku, "unit": p.unit,
            "selling_price": str(p.selling_price), "cost_price": str(p.cost_price),
            "stock_on_hand": str(p.stock_on_hand),
        })


# --------------------------------------------------------------------------- #
# Categories & suppliers
# --------------------------------------------------------------------------- #
class CategoryListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = Category
    template_name = "inventory/category_list.html"
    context_object_name = "categories"
    page_title = "Categories"

    def get_queryset(self):
        return super().get_queryset().annotate(product_count=Count("products"))


class _CategoryEditMixin(PageTitleMixin, RoleRequiredMixin, BusinessScopedQuerysetMixin, SuccessMessageMixin):
    allowed_roles = CATALOG_ROLES
    model = Category
    form_class = CategoryForm
    template_name = "common/form.html"
    success_url = reverse_lazy("inventory:category_list")
    success_message = "Category saved."


class CategoryCreateView(_CategoryEditMixin, CreateView):
    page_title = "New category"


class CategoryUpdateView(_CategoryEditMixin, UpdateView):
    page_title = "Edit category"


class SupplierListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = Supplier
    template_name = "inventory/supplier_list.html"
    context_object_name = "suppliers"
    page_title = "Suppliers"
    page_subtitle = "Supplier directory and delivery performance"

    def get_queryset(self):
        return super().get_queryset().annotate(product_count=Count("products", distinct=True))


class SupplierDetailView(PageTitleMixin, BusinessScopedQuerysetMixin, DetailView):
    model = Supplier
    template_name = "inventory/supplier_detail.html"
    context_object_name = "supplier"

    def get_context_data(self, **kwargs):
        from apps.purchasing.services import supplier_performance

        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.name
        ctx["page_subtitle"] = "Supplier profile"
        ctx["products"] = self.object.products.filter(is_active=True)
        ctx["orders"] = self.object.purchase_orders.order_by("-created_at")[:10]
        ctx["performance"] = supplier_performance(self.object)
        return ctx


class _SupplierEditMixin(PageTitleMixin, RoleRequiredMixin, BusinessScopedQuerysetMixin, SuccessMessageMixin):
    allowed_roles = CATALOG_ROLES
    model = Supplier
    form_class = SupplierForm
    template_name = "common/form.html"
    success_message = "Supplier saved."


class SupplierCreateView(_SupplierEditMixin, CreateView):
    page_title = "New supplier"


class SupplierUpdateView(_SupplierEditMixin, UpdateView):
    page_title = "Edit supplier"


# --------------------------------------------------------------------------- #
# Ledger, adjustments, counts, batches
# --------------------------------------------------------------------------- #
class LedgerView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = InventoryTransaction
    template_name = "inventory/ledger.html"
    context_object_name = "transactions"
    paginate_by = 50
    page_title = "Stock ledger"
    page_subtitle = "Every stock movement, append-only, with running balances"

    def get_queryset(self):
        qs = super().get_queryset().select_related("product", "created_by")
        self.filter_form = LedgerFilterForm(self.request.GET or None, business=self.request.business)
        if self.filter_form.is_valid():
            d = self.filter_form.cleaned_data
            if d.get("product"):
                qs = qs.filter(product=d["product"])
            if d.get("txn_type"):
                qs = qs.filter(txn_type=d["txn_type"])
            if d.get("date_from"):
                qs = qs.filter(occurred_at__date__gte=d["date_from"])
            if d.get("date_to"):
                qs = qs.filter(occurred_at__date__lte=d["date_to"])
        return qs

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            rows = (
                [t.occurred_at.strftime("%Y-%m-%d %H:%M"), t.product.sku, t.product.name, t.get_txn_type_display(),
                 t.quantity, t.balance_after, t.reference, t.notes, t.created_by or ""]
                for t in self.get_queryset().iterator()
            )
            return csv_response(
                "stock_ledger.csv",
                ["Date", "SKU", "Product", "Type", "Quantity", "Balance", "Reference", "Notes", "User"], rows,
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_form"] = self.filter_form
        return ctx


class StockAdjustmentView(PageTitleMixin, RoleRequiredMixin, FormView):
    allowed_roles = STOCK_ROLES
    form_class = StockAdjustmentForm
    template_name = "common/form.html"
    page_title = "Record stock adjustment"
    page_subtitle = "Damages, losses, returns and other non-sale movements"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["business"] = self.request.business
        if self.request.GET.get("product"):
            kwargs.setdefault("initial", {})["product"] = self.request.GET["product"]
        return kwargs

    def form_valid(self, form):
        d = form.cleaned_data
        try:
            txn = services.post_transaction(
                d["product"], d["txn_type"], d["quantity"], user=self.request.user,
                reference=d.get("reference", ""), notes=d["notes"], unit_cost=d["product"].cost_price,
            )
        except ValidationError as exc:
            form.add_error("quantity", exc.messages[0])
            return self.form_invalid(form)
        messages.success(self.request, f"Recorded: {txn}. New balance {fmt_qty(txn.balance_after)}.")
        # Fast feedback: run anomaly screening on the new movement
        from apps.intelligence.services.anomalies import screen_transaction

        screen_transaction(txn)
        return redirect(d["product"].get_absolute_url())


class StockCountView(PageTitleMixin, RoleRequiredMixin, View):
    """Count sheet: enter physical counts for many products at once."""

    allowed_roles = STOCK_ROLES
    template_name = "inventory/stock_count.html"

    def _products(self, request):
        qs = Product.objects.filter(business=request.business, is_active=True).select_related("category")
        if request.GET.get("category"):
            qs = qs.filter(category_id=request.GET["category"])
        return qs

    def get(self, request):
        return render(request, self.template_name, {
            "page_title": "Stock count",
            "page_subtitle": "Enter physical counts; variances are posted as ledger corrections",
            "products": self._products(request),
            "categories": Category.objects.filter(business=request.business),
            "recent_counts": StockCount.objects.filter(business=request.business).select_related("product", "counted_by")[:20],
        })

    def post(self, request):
        notes = request.POST.get("notes", "")[:255]
        posted, variances, errors = 0, 0, []
        for product in self._products(request):
            raw = request.POST.get(f"count_{product.pk}", "").strip()
            if raw == "":
                continue
            try:
                value = Decimal(raw)
                count = services.record_stock_count(product, value, user=request.user, notes=notes)
            except (InvalidOperation, ValidationError) as exc:
                errors.append(f"{product.name}: {getattr(exc, 'messages', ['invalid number'])[0]}")
                continue
            posted += 1
            if count.variance:
                variances += 1
                from apps.intelligence.services.anomalies import screen_stock_count

                screen_stock_count(count)
        for e in errors:
            messages.error(request, e)
        if posted:
            messages.success(request, f"{posted} count(s) recorded, {variances} with variance posted to the ledger.")
        return redirect("inventory:stock_count")


class BatchListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = StockBatch
    template_name = "inventory/batch_list.html"
    context_object_name = "batches"
    paginate_by = 50
    page_title = "Batches & expiry"
    page_subtitle = "Lots with remaining stock, earliest expiry first"

    def get_queryset(self):
        return super().get_queryset().filter(quantity_remaining__gt=0).select_related("product")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        ctx["today"] = today
        ctx["warning_date"] = today + timedelta(days=30)
        return ctx
