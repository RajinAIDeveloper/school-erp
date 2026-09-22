from django.contrib import admin
from .models import StudentAttendance,StaffAttendance,LeaveType,LeaveRequest
from core.admin_base import ReadOnlyAdmin
admin.site.register([StudentAttendance,StaffAttendance,LeaveRequest],ReadOnlyAdmin)
admin.site.register(LeaveType)
