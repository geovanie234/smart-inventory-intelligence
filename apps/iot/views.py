import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView

from apps.accounts.models import User
from apps.common.forms import BusinessModelForm
from apps.common.mixins import BusinessRequiredMixin, BusinessScopedQuerysetMixin, PageTitleMixin, RoleRequiredMixin

from . import services
from .models import IoTDevice, IoTReading

R = User.Role


class DeviceForm(BusinessModelForm):
    business_scoped_fields = ("product",)

    class Meta:
        model = IoTDevice
        fields = ["name", "device_id", "device_type", "product", "location", "unit_weight_grams", "tare_grams",
                  "tolerance_pct", "expected_interval_seconds", "is_active"]


class DeviceListView(PageTitleMixin, BusinessScopedQuerysetMixin, ListView):
    model = IoTDevice
    template_name = "iot/device_list.html"
    context_object_name = "devices"
    page_title = "IoT devices"
    page_subtitle = "Sensors and scanners feeding physical measurements into the system"

    def get_queryset(self):
        return super().get_queryset().select_related("product")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["recent"] = IoTReading.objects.filter(business=self.request.business).select_related("device", "product")[:15]
        return ctx


class DeviceEditView(PageTitleMixin, RoleRequiredMixin, View):
    allowed_roles = (R.MANAGER,)

    def _device(self, request, pk):
        return get_object_or_404(IoTDevice, pk=pk, business=request.business) if pk else None

    def get(self, request, pk=None):
        device = self._device(request, pk)
        form = DeviceForm(instance=device, business=request.business)
        return render(request, "common/form.html", {
            "form": form, "page_title": "Edit device" if device else "Register device",
            "page_subtitle": "An API key is generated on registration and shown once.",
        })

    def post(self, request, pk=None):
        device = self._device(request, pk)
        form = DeviceForm(request.POST, instance=device, business=request.business)
        if not form.is_valid():
            return render(request, "common/form.html", {"form": form, "page_title": "Device"}, status=400)
        obj = form.save(commit=False)
        obj.business = request.business
        raw_key = None
        if not obj.key_hash:
            raw_key = obj.rotate_key()
        obj.save()
        if raw_key:
            request.session["new_device_key"] = raw_key
        messages.success(request, "Device saved.")
        return redirect(obj.get_absolute_url())


class DeviceDetailView(PageTitleMixin, BusinessRequiredMixin, View):
    def get(self, request, pk):
        device = get_object_or_404(IoTDevice.objects.select_related("product"), pk=pk, business=request.business)
        readings = list(device.readings.select_related("product")[:200])
        chart = [
            {"x": r.measured_at.isoformat(), "est": float(r.estimated_quantity), "rec": float(r.recorded_quantity or 0)}
            for r in reversed(readings) if r.estimated_quantity is not None
        ]
        return render(request, "iot/device_detail.html", {
            "page_title": device.name,
            "page_subtitle": f"{device.get_device_type_display()} · {device.device_id}",
            "device": device,
            "readings": readings[:50],
            "chart": chart,
            "new_key": request.session.pop("new_device_key", None),
            "api_url": request.build_absolute_uri("/api/v1/iot/readings/"),
        })


class DeviceRotateKeyView(RoleRequiredMixin, View):
    allowed_roles = (R.MANAGER,)

    def post(self, request, pk):
        device = get_object_or_404(IoTDevice, pk=pk, business=request.business)
        request.session["new_device_key"] = device.rotate_key()
        device.save(update_fields=["key_prefix", "key_hash"])
        messages.warning(request, "New key generated. The old key no longer works — update the device firmware.")
        return redirect(device.get_absolute_url())


class ReadingActionView(RoleRequiredMixin, View):
    allowed_roles = (R.CLERK,)

    def post(self, request, pk):
        reading = get_object_or_404(IoTReading, pk=pk, business=request.business)
        try:
            if request.POST.get("action") == "verify":
                services.verify_reading(reading, request.user)
                messages.success(request, "Reading verified — stock corrected through a count adjustment.")
            else:
                services.dismiss_reading(reading, request.user, request.POST.get("note", ""))
                messages.info(request, "Reading dismissed.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        return redirect(request.POST.get("next") or reading.device.get_absolute_url())
