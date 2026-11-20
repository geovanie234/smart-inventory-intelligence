from .models import Business


class CurrentBusinessMiddleware:
    """
    Attaches `request.business` for the signed-in user.

    Regular users are bound to their own business. Superusers without a business
    can switch between businesses (stored in the session) to administer them.
    """

    SESSION_KEY = "active_business_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.business = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            if user.business_id and user.business.is_active:
                request.business = user.business
            elif user.is_superuser:
                business_id = request.session.get(self.SESSION_KEY)
                qs = Business.objects.filter(is_active=True)
                request.business = (qs.filter(pk=business_id).first() if business_id else None) or qs.first()
        return self.get_response(request)
