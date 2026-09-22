from django.contrib import admin

from .models import AuditLog, School


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "eiin", "phone", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "model", "object_id", "description")
    list_filter = ("action",)
    search_fields = ("description", "object_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]
