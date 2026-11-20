from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import ListView

from apps.accounts.models import User
from apps.common.mixins import BusinessRequiredMixin, BusinessScopedQuerysetMixin, PageTitleMixin, RoleRequiredMixin
from apps.inventory.models import Product
from apps.purchasing.services import create_order

from .models import Alert, DemandPrediction, ForecastRun, ModelEvaluation, ReorderRecommendation
from .services import evaluation

R = User.Role


class ForecastOverviewView(PageTitleMixin, BusinessRequiredMixin, View):
    template_name = "intelligence/forecasts.html"

    def get(self, request):
        business = request.business
        run = evaluation.latest_run(business)
        rows = []
        if run:
            selected = {
                e.product_id: e for e in ModelEvaluation.objects.filter(run=run, product__isnull=False, is_selected=True)
            }
            today = timezone.localdate()
            next7 = dict(
                DemandPrediction.objects.filter(run=run, forecast_date__lt=today + timedelta(days=7))
                .values_list("product_id").annotate(s=Sum("predicted_quantity"))
            )
            fallback_model = dict(DemandPrediction.objects.filter(run=run).values_list("product_id", "model_name").distinct())
            from apps.purchasing.services import on_order_map

            on_order = on_order_map(business)
            for p in Product.objects.filter(business=business, is_active=True).select_related("business", "preferred_supplier"):
                ev = selected.get(p.pk)
                rows.append({
                    "product": p,
                    "model": ev.model_name if ev else fallback_model.get(p.pk, "—"),
                    "wape": ev.wape if ev else None,
                    "mae": ev.mae if ev else None,
                    "next7": next7.get(p.pk),
                    "cover": p.days_of_cover,
                    "on_order": on_order.get(p.pk),
                })
            rows.sort(key=lambda r: (r["cover"] is None, r["cover"] or 0))
        return render(request, self.template_name, {
            "page_title": "Demand forecasts",
            "page_subtitle": "Model comparison on a time-based holdout and per-product forecasts",
            "run": run,
            "comparison": evaluation.model_comparison(run),
            "rows": rows,
            "runs": ForecastRun.objects.filter(business=business)[:8],
        })


class RunIntelligenceView(RoleRequiredMixin, View):
    allowed_roles = (R.MANAGER, R.PURCHASING)

    def post(self, request):
        from .services.pipeline import run_intelligence  # heavy ML imports only when needed

        try:
            result = run_intelligence(request.business, user=request.user)
        except Exception as exc:  # surface failures to the user, details are logged
            messages.error(request, f"Forecasting failed: {exc}")
            return redirect("intelligence:forecasts")
        run = result["run"]
        messages.success(
            request,
            f"Forecast run #{run.pk} finished in {run.duration_seconds:.1f}s: {run.products_forecast} products modelled, "
            f"{result['recommendations']} reorder recommendation(s), "
            f"{result['new_stock_alerts'] + result['new_expiry_alerts'] + result['new_anomalies']} new alert(s).",
        )
        return redirect(request.POST.get("next") or "intelligence:forecasts")


class RecommendationListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = ReorderRecommendation
    template_name = "intelligence/recommendations.html"
    context_object_name = "recommendations"
    page_title = "Reorder recommendations"
    page_subtitle = "Explainable order suggestions — a manager approves every purchase order"

    def get_queryset(self):
        status = self.request.GET.get("status", ReorderRecommendation.Status.PENDING)
        order = {"critical": 0, "out": 0, "low": 1, "ok": 2, "overstock": 3, "unknown": 4}
        recs = list(
            super().get_queryset().filter(status=status).select_related("product", "supplier", "product__business")
        )
        recs.sort(key=lambda r: (order.get(r.risk_level, 9), r.days_of_cover or 0))
        return recs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        recs = ctx["recommendations"]
        ctx["status"] = self.request.GET.get("status", ReorderRecommendation.Status.PENDING)
        ctx["statuses"] = ReorderRecommendation.Status.choices
        ctx["total_cost"] = sum(r.estimated_cost for r in recs)
        return ctx


class RecommendationActionView(RoleRequiredMixin, View):
    allowed_roles = (R.PURCHASING,)

    def post(self, request):
        ids = request.POST.getlist("selected")
        action = request.POST.get("action")
        recs = list(
            ReorderRecommendation.objects.filter(
                business=request.business, pk__in=ids, status=ReorderRecommendation.Status.PENDING
            ).select_related("product", "supplier")
        )
        if not recs:
            messages.warning(request, "Select at least one pending recommendation.")
            return redirect("intelligence:recommendations")
        now = timezone.now()
        if action == "dismiss":
            for r in recs:
                r.status, r.decided_by, r.decided_at = ReorderRecommendation.Status.DISMISSED, request.user, now
                r.save(update_fields=["status", "decided_by", "decided_at"])
            messages.info(request, f"{len(recs)} recommendation(s) dismissed.")
            return redirect("intelligence:recommendations")

        without_supplier = [r for r in recs if r.supplier is None]
        if without_supplier:
            messages.error(
                request, "Set a preferred supplier for: " + ", ".join(r.product.name for r in without_supplier)
            )
            return redirect("intelligence:recommendations")
        by_supplier = defaultdict(list)
        for r in recs:
            qty_override = request.POST.get(f"qty_{r.pk}")
            try:
                qty = float(qty_override) if qty_override else float(r.recommended_quantity)
            except ValueError:
                qty = float(r.recommended_quantity)
            by_supplier[r.supplier].append((r, qty))
        created = []
        try:
            with transaction.atomic():
                for supplier, items in by_supplier.items():
                    order = create_order(
                        request.business, supplier,
                        [(r.product, Decimal(str(q)), r.product.cost_price) for r, q in items],
                        user=request.user, notes="Generated from AI reorder recommendations.",
                    )
                    lines = {i.product_id: i for i in order.items.all()}
                    for r, _ in items:
                        r.status, r.decided_by, r.decided_at = ReorderRecommendation.Status.IN_DRAFT, request.user, now
                        r.purchase_item = lines.get(r.product_id)
                        r.save(update_fields=["status", "decided_by", "decided_at", "purchase_item"])
                    created.append(order)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("intelligence:recommendations")
        messages.success(
            request, f"Created {len(created)} draft purchase order(s): " + ", ".join(o.reference for o in created)
            + ". Submit them for manager approval."
        )
        return redirect("purchasing:po_list")


class AlertListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = Alert
    template_name = "intelligence/alerts.html"
    context_object_name = "alerts"
    paginate_by = 40
    page_title = "Alerts"
    page_subtitle = "Stock risks, anomalies, expiry and sensor discrepancies"

    def get_queryset(self):
        qs = super().get_queryset().select_related("product", "transaction", "iot_reading")
        status = self.request.GET.get("status", "active")
        if status == "active":
            qs = qs.filter(status__in=[Alert.Status.OPEN, Alert.Status.ACKNOWLEDGED])
        elif status:
            qs = qs.filter(status=status)
        if t := self.request.GET.get("type"):
            qs = qs.filter(alert_type=t)
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        return sorted(qs, key=lambda a: (a.status != Alert.Status.OPEN, severity_rank.get(a.severity, 3), -a.created_at.timestamp()))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["types"] = Alert.Type.choices
        ctx["status"] = self.request.GET.get("status", "active")
        ctx["type"] = self.request.GET.get("type", "")
        ctx["type_counts"] = dict(
            Alert.objects.filter(business=self.request.business, status=Alert.Status.OPEN)
            .values_list("alert_type").annotate(n=Count("id"))
        )
        return ctx


class AlertActionView(BusinessRequiredMixin, View):
    def post(self, request, pk):
        alert = get_object_or_404(Alert, pk=pk, business=request.business)
        action = request.POST.get("action")
        if action == "acknowledge":
            alert.status = Alert.Status.ACKNOWLEDGED
        elif action == "resolve":
            alert.status = Alert.Status.RESOLVED
            alert.resolved_at, alert.resolved_by = timezone.now(), request.user
            alert.resolution_note = request.POST.get("note", "")[:255]
        elif action in (Alert.Feedback.USEFUL, Alert.Feedback.FALSE_ALARM):
            alert.feedback = action
        else:
            messages.error(request, "Unknown action.")
            return redirect("intelligence:alerts")
        alert.save()
        messages.success(request, "Alert updated.")
        return redirect(request.POST.get("next") or "intelligence:alerts")


class EvaluationView(PageTitleMixin, BusinessRequiredMixin, View):
    template_name = "intelligence/evaluation.html"

    def get(self, request):
        business = request.business
        run = evaluation.latest_run(business)
        return render(request, self.template_name, {
            "page_title": "System evaluation",
            "page_subtitle": "Forecast error, alert quality and stock-record accuracy (proposal §13)",
            "run": run,
            "comparison": evaluation.model_comparison(run),
            "realised": evaluation.realised_accuracy(business),
            "alerts": evaluation.alert_quality(business),
            "stock": evaluation.stock_record_accuracy(business),
        })
