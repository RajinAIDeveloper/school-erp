from django.contrib import admin

from .models import Department, Designation, Employee


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "full_name", "employee_type", "designation", "status", "school")
    list_filter = ("school", "employee_type", "status")
    search_fields = ("employee_id", "first_name", "last_name", "phone")


admin.site.register([Department, Designation])
