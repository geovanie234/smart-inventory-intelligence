from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Business, User


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "business_type", "currency", "is_demo", "is_active", "created_at")
    list_filter = ("business_type", "is_active", "is_demo")
    search_fields = ("name", "tin", "address")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "business", "role", "is_active", "is_staff")
    list_filter = ("role", "business", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (("Business access", {"fields": ("business", "role", "phone")}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Business access", {"fields": ("business", "role")}),)
