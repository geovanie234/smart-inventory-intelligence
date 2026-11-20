from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.models import User
from apps.common.mixins import BusinessRequiredMixin, BusinessScopedQuerysetMixin, PageTitleMixin, RoleRequiredMixin
from apps.common.utils import csv_response
from apps.inventory.models import Product

from .models import Sale, SaleItem
from .services import SaleLine, record_sale, void_sale


def _date_range(request, default_days=30):
    today = timezone.localdate()
    start = parse_date(request.GET.get("from") or "") or today - timedelta(days=default_days - 1)
    end = parse_date(request.GET.get("to") or "") or today
    if start > end:
        start, end = end, start
    return start, end


class SaleListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = Sale
    template_name = "sales/sale_list.html"
    context_object_name = "sales"
    paginate_by = 30
    page_title = "Sales"
    page_subtitle = "Recorded sales transactions"

    def get_queryset(self):
        self.start, self.end = _date_range(self.request)
        qs = (
            super().get_queryset()
            .filter(sold_at__date__gte=self.start, sold_at__date__lte=self.end)
            .select_related("cashier")
            .annotate(n_items=Count("items"))
            .order_by("-sold_at", "-id")
        )
        if q := self.request.GET.get("q"):
            qs = qs.filter(Q(reference__icontains=q) | Q(customer_name__icontains=q))
        return qs

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            items = SaleItem.objects.filter(sale__in=self.get_queryset()).select_related("sale", "product")
            rows = (
                [i.sale.reference, i.sale.sold_at.strftime("%Y-%m-%d %H:%M"), i.sale.get_status_display(),
                 i.sale.get_payment_method_display(), i.product.sku, i.product.name, i.quantity, i.unit_price,
                 i.line_total]
                for i in items.iterator()
            )
            return csv_response(
                f"sales_{self.start}_{self.end}.csv",
                ["Reference", "Date", "Status", "Payment", "SKU", "Product", "Qty", "Unit price", "Line total"], rows,
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        completed = self.get_queryset().filter(status=Sale.Status.COMPLETED)
        ctx.update(
            start=self.start, end=self.end,
            period_total=completed.aggregate(t=Sum("total_amount"))["t"] or 0,
            period_count=completed.count(),
        )
        return ctx


class SaleDetailView(PageTitleMixin, BusinessScopedQuerysetMixin, DetailView):
    model = Sale
    template_name = "sales/sale_detail.html"
    context_object_name = "sale"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Sale {self.object.reference}"
        ctx["items"] = self.object.items.select_related("product")
        return ctx


class SaleCreateView(RoleRequiredMixin, View):
    """Point-of-sale style entry: scan barcodes or pick products, then confirm."""

    allowed_roles = (User.Role.CLERK,)
    template_name = "sales/sale_form.html"

    def _ctx(self, request, **extra):
        products = Product.objects.filter(business=request.business, is_active=True).only(
            "id", "name", "sku", "barcode", "selling_price", "stock_on_hand", "unit"
        )
        return {
            "page_title": "New sale",
            "page_subtitle": "Scan a barcode or choose products",
            "products": products,
            "payment_methods": Sale.PaymentMethod.choices,
            **extra,
        }

    def get(self, request):
        return render(request, self.template_name, self._ctx(request))

    def post(self, request):
        product_ids = request.POST.getlist("product")
        quantities = request.POST.getlist("quantity")
        prices = request.POST.getlist("unit_price")
        products = {p.pk: p for p in Product.objects.filter(business=request.business, pk__in=[i for i in product_ids if i.isdigit()])}
        lines, errors = [], []
        for pid, qty, price in zip(product_ids, quantities, prices):
            if not pid:
                continue
            product = products.get(int(pid)) if pid.isdigit() else None
            if product is None:
                errors.append("Unknown product selected.")
                continue
            try:
                q = Decimal(qty)
                p = Decimal(price) if price else None
                if p is not None and p < 0:
                    raise InvalidOperation
            except (InvalidOperation, TypeError):
                errors.append(f"Invalid quantity or price for {product.name}.")
                continue
            lines.append(SaleLine(product, q, p))
        if not errors:
            try:
                sale = record_sale(
                    request.business, lines, user=request.user,
                    payment_method=request.POST.get("payment_method") or Sale.PaymentMethod.CASH,
                    customer_name=request.POST.get("customer_name", "")[:120],
                    notes=request.POST.get("notes", "")[:255],
                )
            except ValidationError as exc:
                errors.extend(exc.messages)
            else:
                messages.success(request, f"Sale {sale.reference} recorded — {request.business.currency} {sale.total_amount:,.0f}.")
                return redirect(sale.get_absolute_url())
        for e in errors:
            messages.error(request, e)
        return render(request, self.template_name, self._ctx(request), status=400)


class SaleVoidView(RoleRequiredMixin, View):
    allowed_roles = (User.Role.MANAGER,)

    def post(self, request, pk):
        sale = get_object_or_404(Sale, pk=pk, business=request.business)
        try:
            void_sale(sale, user=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, f"Sale {sale.reference} voided and stock returned.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect(sale.get_absolute_url())


class SalesReportView(PageTitleMixin, BusinessRequiredMixin, View):
    template_name = "sales/report.html"

    def get(self, request):
        start, end = _date_range(request)
        items = SaleItem.objects.filter(
            sale__business=request.business, sale__status=Sale.Status.COMPLETED,
            sale__sold_at__date__gte=start, sale__sold_at__date__lte=end,
        )
        margin_expr = ExpressionWrapper(
            F("line_total") - F("unit_cost") * F("quantity"), output_field=DecimalField(max_digits=16, decimal_places=2)
        )
        by_product = (
            items.values("product__id", "product__name", "product__sku", "product__category__name")
            .annotate(qty=Sum("quantity"), revenue=Sum("line_total"), margin=Sum(margin_expr))
            .order_by("-revenue")
        )
        by_category = (
            items.values("product__category__name").annotate(revenue=Sum("line_total")).order_by("-revenue")
        )
        daily = dict(
            items.annotate(day=TruncDate("sale__sold_at")).values_list("day").annotate(r=Sum("line_total"))
        )
        days = (end - start).days + 1
        series = [
            {"x": (start + timedelta(days=i)).isoformat(), "y": float(daily.get(start + timedelta(days=i), 0))}
            for i in range(days)
        ]
        totals = items.aggregate(revenue=Sum("line_total"), margin=Sum(margin_expr), qty=Sum("quantity"))

        if request.GET.get("export") == "csv":
            return csv_response(
                f"sales_by_product_{start}_{end}.csv",
                ["SKU", "Product", "Category", "Units", "Revenue", "Gross margin"],
                ([r["product__sku"], r["product__name"], r["product__category__name"] or "", r["qty"],
                  r["revenue"], r["margin"]] for r in by_product),
            )
        return render(request, self.template_name, {
            "page_title": "Sales report",
            "page_subtitle": f"{start:%d %b %Y} – {end:%d %b %Y}",
            "start": start, "end": end, "by_product": by_product[:50],
            "category_chart": [
                {"label": r["product__category__name"] or "Uncategorised", "value": float(r["revenue"])}
                for r in by_category
            ],
            "series": series,
            "totals": totals,
            "transactions": Sale.objects.filter(
                business=request.business, status=Sale.Status.COMPLETED,
                sold_at__date__gte=start, sold_at__date__lte=end,
            ).count(),
        })
