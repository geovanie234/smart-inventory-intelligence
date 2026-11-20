from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("categories", views.CategoryViewSet, basename="category")
router.register("suppliers", views.SupplierViewSet, basename="supplier")
router.register("products", views.ProductViewSet, basename="product")
router.register("transactions", views.TransactionViewSet, basename="transaction")
router.register("sales", views.SaleViewSet, basename="sale")
router.register("recommendations", views.RecommendationViewSet, basename="recommendation")
router.register("alerts", views.AlertViewSet, basename="alert")

app_name = "api"

urlpatterns = [
    path("auth/token/", obtain_auth_token, name="token"),
    path("iot/readings/", views.IoTReadingIngestView.as_view(), name="iot_readings"),
    path("dashboard/summary/", views.dashboard_summary, name="dashboard_summary"),
    path("", include(router.urls)),
]
