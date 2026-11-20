from django.shortcuts import render
from django.views import View

from apps.common.mixins import BusinessRequiredMixin
from apps.intelligence.models import Alert, ReorderRecommendation
from apps.intelligence.services.evaluation import latest_run
from apps.inventory.models import Product
from apps.sales.models import Sale

from . import services

RISK_ORDER = ["out", "critical", "low", "ok", "overstock", "unknown"]


class DashboardView(BusinessRequiredMixin, View):
    def get(self, request):
        b = request.business
        k = services.kpis(b)
        labels = dict(Product.Risk.choices)
        risk_chart = [{"key": r, "label": labels[r], "value": k["risk_counts"].get(r, 0)} for r in RISK_ORDER]
        top = services.top_products(b)
        return render(request, "dashboard/home.html", {
            "page_title": "Dashboard",
            "page_subtitle": f"{b.name} · inventory health and demand outlook",
            "k": k,
            "series": services.sales_and_forecast_series(b),
            "risk_chart": risk_chart,
            "top_chart": [{"label": t["product__name"], "value": float(t["revenue"]), "qty": float(t["qty"])} for t in top],
            "alerts": Alert.objects.filter(business=b, status=Alert.Status.OPEN).select_related("product").order_by("-created_at")[:6],
            "recommendations": ReorderRecommendation.objects.filter(
                business=b, status=ReorderRecommendation.Status.PENDING
            ).select_related("product").order_by("days_of_cover")[:6],
            "recent_sales": Sale.objects.filter(business=b).select_related("cashier")[:6],
            "run": latest_run(b),
        })
