from django.contrib import admin

from core.admin_base import ReadOnlyAdmin

from .models import LeaveRequest, LeaveType, StaffAttendance, StudentAttendance

admin.site.register([StudentAttendance, StaffAttendance, LeaveRequest], ReadOnlyAdmin)
admin.site.register(LeaveType)
