from django.contrib.auth.models import AnonymousUser
from rest_framework import authentication, exceptions, permissions, throttling

from apps.iot.models import IoTDevice
from apps.iot.services import authenticate_device


class DeviceKeyAuthentication(authentication.BaseAuthentication):
    """
    Devices authenticate with two headers:
        X-Device-Id:  <device_id>
        Authorization: Device <api key>
    The request is anonymous as a user; `request.auth` is the IoTDevice.
    """

    keyword = "Device"

    def authenticate(self, request):
        device_id = request.headers.get("X-Device-Id")
        header = request.headers.get("Authorization", "")
        if not device_id or not header.startswith(self.keyword + " "):
            return None
        device = authenticate_device(device_id, header[len(self.keyword) + 1:].strip())
        if device is None:
            raise exceptions.AuthenticationFailed("Invalid device id or key.")
        return AnonymousUser(), device

    def authenticate_header(self, request):
        return self.keyword


class IsAuthenticatedDevice(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, IoTDevice)


class DeviceRateThrottle(throttling.SimpleRateThrottle):
    scope = "iot_device"

    def get_cache_key(self, request, view):
        if isinstance(request.auth, IoTDevice):
            return self.cache_format % {"scope": self.scope, "ident": request.auth.device_id}
        return self.get_ident(request)
