from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from apps.common.forms import BootstrapFormMixin

from .models import Business, User


class LoginForm(BootstrapFormMixin, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"placeholder": "Username", "autofocus": True})
        self.fields["password"].widget.attrs.update({"placeholder": "Password"})
        self._apply_bootstrap()


class StaffCreateForm(BootstrapFormMixin, UserCreationForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "role"]

    def __init__(self, *args, business=None, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def save(self, commit=True):
        user = super().save(commit=False)
        user.business = self.business
        if commit:
            user.save()
        return user


class StaffUpdateForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone", "role", "is_active"]

    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class BusinessSettingsForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Business
        fields = [
            "name", "business_type", "address", "phone", "email", "tin", "currency",
            "review_period_days", "default_lead_time_days", "overstock_cover_days",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
