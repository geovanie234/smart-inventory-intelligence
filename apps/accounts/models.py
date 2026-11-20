from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.text import slugify


class Business(models.Model):
    class BusinessType(models.TextChoices):
        RETAIL_SHOP = "retail_shop", "Retail shop"
        SUPERMARKET = "supermarket", "Supermarket"
        WHOLESALER = "wholesaler", "Wholesaler"
        WAREHOUSE = "warehouse", "Warehouse"
        DISTRIBUTOR = "distributor", "Distributor"
        PHARMACY = "pharmacy", "Pharmacy"
        OTHER = "other", "Other"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True)
    business_type = models.CharField(max_length=30, choices=BusinessType.choices, default=BusinessType.RETAIL_SHOP)
    address = models.CharField(max_length=255, blank=True, help_text="e.g. KN 4 Ave, Nyarugenge, Kigali")
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    tin = models.CharField("TIN", max_length=30, blank=True, help_text="Rwanda Revenue Authority tax ID")
    currency = models.CharField(max_length=3, default="RWF")

    # Replenishment policy defaults (overridable per product)
    review_period_days = models.PositiveSmallIntegerField(
        default=7, help_text="How often purchasing is reviewed (periodic-review policy)."
    )
    default_lead_time_days = models.PositiveSmallIntegerField(default=3)
    overstock_cover_days = models.PositiveSmallIntegerField(
        default=60, help_text="Stock covering more days of forecast demand than this is flagged as overstock."
    )
    is_demo = models.BooleanField(default=False, help_text="Contains clearly labelled synthetic data.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "businesses"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:150] or "business"
            slug, i = base, 2
            while Business.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "System administrator"
        MANAGER = "manager", "Owner / manager"
        PURCHASING = "purchasing", "Purchasing officer"
        CLERK = "clerk", "Inventory clerk"

    business = models.ForeignKey(Business, on_delete=models.PROTECT, null=True, blank=True, related_name="users")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLERK)
    phone = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["username"]

    # Managers and administrators may perform every business action.
    PRIVILEGED_ROLES = {Role.ADMIN, Role.MANAGER}

    def has_role(self, *roles) -> bool:
        if self.is_superuser or self.role in self.PRIVILEGED_ROLES:
            return True
        return self.role in roles

    @property
    def is_manager(self) -> bool:
        return self.is_superuser or self.role in self.PRIVILEGED_ROLES

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username

    @property
    def initials(self) -> str:
        parts = [p for p in [self.first_name, self.last_name] if p]
        if parts:
            return "".join(p[0] for p in parts).upper()[:2]
        return self.username[:2].upper()
