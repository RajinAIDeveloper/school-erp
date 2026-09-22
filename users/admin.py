from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class ERPUserAdmin(UserAdmin):
    list_display = ("username", "first_name", "last_name", "school", "is_staff", "is_active")
    list_filter = ("school", "is_staff", "is_active", "groups")
    fieldsets = UserAdmin.fieldsets + (("School & contact", {"fields": ("school", "phone", "avatar")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("School & contact", {"fields": ("school", "phone")}),)
