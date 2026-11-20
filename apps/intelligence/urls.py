from django.urls import path

from . import views

app_name = "intelligence"

urlpatterns = [
    path("forecasts/", views.ForecastOverviewView.as_view(), name="forecasts"),
    path("run/", views.RunIntelligenceView.as_view(), name="run"),
    path("recommendations/", views.RecommendationListView.as_view(), name="recommendations"),
    path("recommendations/action/", views.RecommendationActionView.as_view(), name="recommendation_action"),
    path("alerts/", views.AlertListView.as_view(), name="alerts"),
    path("alerts/<int:pk>/action/", views.AlertActionView.as_view(), name="alert_action"),
    path("evaluation/", views.EvaluationView.as_view(), name="evaluation"),
]
