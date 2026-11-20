from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Smart Inventory Intelligence — Administration"
admin.site.site_title = "SIIS Admin"
admin.site.index_title = "System administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("sales/", include("apps.sales.urls")),
    path("purchasing/", include("apps.purchasing.urls")),
    path("intelligence/", include("apps.intelligence.urls")),
    path("iot/", include("apps.iot.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("", include("apps.dashboard.urls")),
]
