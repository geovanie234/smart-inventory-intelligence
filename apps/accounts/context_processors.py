def business_context(request):
    business = getattr(request, "business", None)
    ctx = {"current_business": business}
    if business is not None and request.user.is_authenticated:
        from apps.intelligence.models import Alert

        ctx["open_alert_count"] = Alert.objects.filter(business=business, status=Alert.Status.OPEN).count()
        if request.user.is_superuser and not request.user.business_id:
            from .models import Business

            ctx["switchable_businesses"] = Business.objects.filter(is_active=True)
    return ctx
