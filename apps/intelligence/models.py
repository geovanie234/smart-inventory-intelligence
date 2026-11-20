from django.conf import settings
from django.db import models

from apps.common.models import BusinessOwnedModel
from apps.inventory.models import QTY, Product


class ForecastRun(BusinessOwnedModel):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Completed"
        FAILED = "failed", "Failed"

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    horizon_days = models.PositiveSmallIntegerField()
    holdout_days = models.PositiveSmallIntegerField()
    history_start = models.DateField(null=True, blank=True)
    history_end = models.DateField(null=True, blank=True)
    products_forecast = models.PositiveIntegerField(default=0)
    products_insufficient = models.PositiveIntegerField(default=0)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Forecast run #{self.pk} ({self.get_status_display()})"

    @property
    def duration_seconds(self):
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class ModelEvaluation(models.Model):
    """Time-based holdout error of one candidate model (per product, or portfolio-level when product is null)."""

    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="evaluations")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    model_name = models.CharField(max_length=60)
    mae = models.FloatField()
    wape = models.FloatField(null=True, help_text="Weighted absolute percentage error (%).")
    bias = models.FloatField(default=0, help_text="Mean (forecast − actual); positive = over-forecasting.")
    n_days = models.PositiveIntegerField()
    is_baseline = models.BooleanField(default=False)
    is_selected = models.BooleanField(default=False)

    class Meta:
        ordering = ["wape", "mae"]
        indexes = [models.Index(fields=["run", "product"])]

    def __str__(self):
        return f"{self.model_name}: MAE {self.mae:.2f}, WAPE {self.wape or 0:.1f}%"


class DemandPrediction(BusinessOwnedModel):
    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="predictions")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="predictions")
    forecast_date = models.DateField(db_index=True)
    predicted_quantity = models.DecimalField(**QTY)
    lower_bound = models.DecimalField(**QTY)
    upper_bound = models.DecimalField(**QTY)
    model_name = models.CharField(max_length=60)

    class Meta:
        ordering = ["forecast_date"]
        constraints = [
            models.UniqueConstraint(fields=["run", "product", "forecast_date"], name="uniq_prediction_per_run_day")
        ]

    def __str__(self):
        return f"{self.product.name} {self.forecast_date}: {self.predicted_quantity}"


class ReorderRecommendation(BusinessOwnedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        ORDERED = "ordered", "Ordered"
        IN_DRAFT = "in_draft", "Added to draft PO"
        DISMISSED = "dismissed", "Dismissed"
        SUPERSEDED = "superseded", "Superseded"

    run = models.ForeignKey(ForecastRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="recommendations")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="recommendations")
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    risk_level = models.CharField(max_length=12, choices=Product.Risk.choices)

    # Inputs (stored so every recommendation can be explained and audited)
    current_stock = models.DecimalField(**QTY)
    on_order = models.DecimalField(**QTY)
    avg_daily_demand = models.DecimalField(**QTY)
    demand_std = models.DecimalField(**QTY, default=0)
    lead_time_days = models.PositiveSmallIntegerField()
    review_period_days = models.PositiveSmallIntegerField()
    safety_stock = models.DecimalField(**QTY)
    safety_stock_method = models.CharField(max_length=20, default="configured")
    # Outputs
    expected_demand = models.DecimalField(**QTY)
    target_level = models.DecimalField(**QTY)
    raw_quantity = models.DecimalField(**QTY)
    recommended_quantity = models.DecimalField(**QTY)
    days_of_cover = models.FloatField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    explanation = models.JSONField(default=list)

    purchase_item = models.ForeignKey(
        "purchasing.PurchaseItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="recommendations"
    )
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reorder {self.recommended_quantity} × {self.product.name}"

    @property
    def risk_badge(self):
        return Product.RISK_BADGE.get(self.risk_level, "secondary")


class Alert(BusinessOwnedModel):
    class Type(models.TextChoices):
        OUT_OF_STOCK = "out_of_stock", "Out of stock"
        STOCKOUT_RISK = "stockout_risk", "Stockout risk"
        OVERSTOCK = "overstock", "Overstock"
        ANOMALY = "anomaly", "Unusual stock movement"
        EXPIRY = "expiry", "Expiry risk"
        IOT_DISCREPANCY = "iot_discrepancy", "Sensor discrepancy"
        DEVICE_OFFLINE = "device_offline", "Device offline"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    class Feedback(models.TextChoices):
        USEFUL = "useful", "Correct / useful"
        FALSE_ALARM = "false_alarm", "False alarm"

    SEVERITY_BADGE = {"info": "info", "warning": "warning", "critical": "danger"}
    TYPE_ICON = {
        "out_of_stock": "bi-x-octagon", "stockout_risk": "bi-exclamation-triangle", "overstock": "bi-boxes",
        "anomaly": "bi-activity", "expiry": "bi-calendar-x", "iot_discrepancy": "bi-cpu",
        "device_offline": "bi-wifi-off",
    }

    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts")
    alert_type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARNING)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True)
    title = models.CharField(max_length=200)
    message = models.TextField()
    dedupe_key = models.CharField(max_length=120, db_index=True)
    score = models.FloatField(null=True, blank=True, help_text="Model score / statistic that triggered the alert.")
    transaction = models.ForeignKey(
        "inventory.InventoryTransaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )
    iot_reading = models.ForeignKey("iot.IoTReading", on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts")
    last_seen_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolution_note = models.CharField(max_length=255, blank=True)
    feedback = models.CharField(max_length=12, choices=Feedback.choices, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["business", "status", "alert_type"])]

    def __str__(self):
        return self.title

    @property
    def severity_badge(self):
        return self.SEVERITY_BADGE.get(self.severity, "secondary")

    @property
    def icon(self):
        return self.TYPE_ICON.get(self.alert_type, "bi-bell")
