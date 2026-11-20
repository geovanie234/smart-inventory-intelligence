from django.contrib import admin

from .models import Alert, DemandPrediction, ForecastRun, ModelEvaluation, ReorderRecommendation


@admin.register(ForecastRun)
class ForecastRunAdmin(admin.ModelAdmin):
    list_display = ("id", "business", "status", "started_at", "products_forecast", "products_insufficient")
    list_filter = ("business", "status")
    readonly_fields = ("summary",)


@admin.register(ModelEvaluation)
class ModelEvaluationAdmin(admin.ModelAdmin):
    list_display = ("run", "product", "model_name", "mae", "wape", "bias", "n_days", "is_baseline", "is_selected")
    list_filter = ("model_name", "is_selected")


@admin.register(DemandPrediction)
class DemandPredictionAdmin(admin.ModelAdmin):
    list_display = ("product", "forecast_date", "predicted_quantity", "lower_bound", "upper_bound", "model_name", "run")
    list_filter = ("business", "model_name")
    date_hierarchy = "forecast_date"


@admin.register(ReorderRecommendation)
class ReorderRecommendationAdmin(admin.ModelAdmin):
    list_display = ("product", "recommended_quantity", "risk_level", "status", "created_at")
    list_filter = ("business", "status", "risk_level")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "business", "alert_type", "severity", "status", "feedback", "created_at")
    list_filter = ("business", "alert_type", "severity", "status", "feedback")
    search_fields = ("title", "message")
