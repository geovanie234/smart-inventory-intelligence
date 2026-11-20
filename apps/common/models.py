from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BusinessOwnedModel(TimeStampedModel):
    """Every business-owned record carries a Business foreign key (multi-tenant design)."""

    business = models.ForeignKey(
        "accounts.Business", on_delete=models.CASCADE, related_name="%(app_label)s_%(class)s_set"
    )

    class Meta:
        abstract = True
