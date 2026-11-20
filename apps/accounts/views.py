from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from apps.common.mixins import PageTitleMixin, RoleRequiredMixin

from .forms import BusinessSettingsForm, LoginForm, StaffCreateForm, StaffUpdateForm
from .middleware import CurrentBusinessMiddleware
from .models import Business, User


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    pass


@login_required
def no_business(request):
    return render(request, "accounts/no_business.html", {"page_title": "No business assigned"})


@login_required
@require_POST
def switch_business(request):
    if not request.user.is_superuser:
        return redirect("dashboard:home")
    business = get_object_or_404(Business, pk=request.POST.get("business_id"), is_active=True)
    request.session[CurrentBusinessMiddleware.SESSION_KEY] = business.pk
    messages.success(request, f"Now viewing {business.name}.")
    return redirect(request.POST.get("next") or "dashboard:home")


class StaffListView(PageTitleMixin, RoleRequiredMixin, ListView):
    allowed_roles = (User.Role.MANAGER,)
    template_name = "accounts/staff_list.html"
    context_object_name = "staff"
    page_title = "Team"
    page_subtitle = "People with access to this business and their roles"

    def get_queryset(self):
        return User.objects.filter(business=self.request.business).order_by("role", "username")


class StaffCreateView(PageTitleMixin, RoleRequiredMixin, SuccessMessageMixin, CreateView):
    allowed_roles = (User.Role.MANAGER,)
    form_class = StaffCreateForm
    template_name = "common/form.html"
    success_url = reverse_lazy("accounts:staff_list")
    success_message = "User %(username)s created."
    page_title = "Add team member"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["business"] = self.request.business
        return kwargs


class StaffUpdateView(PageTitleMixin, RoleRequiredMixin, SuccessMessageMixin, UpdateView):
    allowed_roles = (User.Role.MANAGER,)
    form_class = StaffUpdateForm
    template_name = "common/form.html"
    success_url = reverse_lazy("accounts:staff_list")
    success_message = "User updated."
    page_title = "Edit team member"

    def get_queryset(self):
        return User.objects.filter(business=self.request.business)


class BusinessSettingsView(PageTitleMixin, RoleRequiredMixin, SuccessMessageMixin, UpdateView):
    allowed_roles = (User.Role.MANAGER,)
    form_class = BusinessSettingsForm
    template_name = "common/form.html"
    success_url = reverse_lazy("accounts:business_settings")
    success_message = "Business settings saved."
    page_title = "Business settings"
    page_subtitle = "Profile and default replenishment policy"

    def get_object(self, queryset=None):
        return self.request.business
