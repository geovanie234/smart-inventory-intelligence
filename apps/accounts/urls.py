from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("no-business/", views.no_business, name="no_business"),
    path("switch-business/", views.switch_business, name="switch_business"),
    path("team/", views.StaffListView.as_view(), name="staff_list"),
    path("team/add/", views.StaffCreateView.as_view(), name="staff_create"),
    path("team/<int:pk>/edit/", views.StaffUpdateView.as_view(), name="staff_update"),
    path("settings/", views.BusinessSettingsView.as_view(), name="business_settings"),
]
