"""View mixins enforcing business scoping and role-based access."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


class BusinessRequiredMixin(LoginRequiredMixin):
    """Requires an authenticated user who belongs to an active business."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if getattr(request, "business", None) is None:
            messages.error(request, "Your account is not linked to a business. Contact your administrator.")
            return redirect("accounts:no_business")
        return super().dispatch(request, *args, **kwargs)


class RoleRequiredMixin(BusinessRequiredMixin):
    """Restricts a view to the listed roles. Managers and administrators always pass."""

    allowed_roles: tuple = ()

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and self.allowed_roles and not user.has_role(*self.allowed_roles):
            raise PermissionDenied("Your role does not permit this action.")
        return super().dispatch(request, *args, **kwargs)


class BusinessScopedQuerysetMixin(BusinessRequiredMixin):
    """Filters querysets to the current business and stamps new objects with it."""

    def get_queryset(self):
        return super().get_queryset().filter(business=self.request.business)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["business"] = self.request.business
        return kwargs

    def form_valid(self, form):
        if hasattr(form.instance, "business_id") and not form.instance.business_id:
            form.instance.business = self.request.business
        return super().form_valid(form)


class PageTitleMixin:
    page_title = ""
    page_subtitle = ""

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("page_title", self.page_title)
        ctx.setdefault("page_subtitle", self.page_subtitle)
        return ctx
