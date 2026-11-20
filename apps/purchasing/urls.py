from django.urls import path

from . import views

app_name = "purchasing"

urlpatterns = [
    path("", views.PurchaseOrderListView.as_view(), name="po_list"),
    path("new/", views.PurchaseOrderCreateView.as_view(), name="po_create"),
    path("<int:pk>/", views.PurchaseOrderDetailView.as_view(), name="po_detail"),
    path("<int:pk>/receive/", views.PurchaseOrderReceiveView.as_view(), name="po_receive"),
    path("<int:pk>/<str:action>/", views.PurchaseOrderActionView.as_view(), name="po_action"),
]
