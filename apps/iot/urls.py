from django.urls import path

from . import views

app_name = "iot"

urlpatterns = [
    path("", views.DeviceListView.as_view(), name="device_list"),
    path("devices/new/", views.DeviceEditView.as_view(), name="device_create"),
    path("devices/<int:pk>/", views.DeviceDetailView.as_view(), name="device_detail"),
    path("devices/<int:pk>/edit/", views.DeviceEditView.as_view(), name="device_update"),
    path("devices/<int:pk>/rotate-key/", views.DeviceRotateKeyView.as_view(), name="device_rotate_key"),
    path("readings/<int:pk>/action/", views.ReadingActionView.as_view(), name="reading_action"),
]
