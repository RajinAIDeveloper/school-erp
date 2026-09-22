from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.views.generic import ListView, UpdateView

from academics.models import AcademicYear
from attendance.models import AttendanceStatus, LeaveRequest, StaffAttendance, StudentAttendance
from employees.models import Employee
from fees.models import FeeInvoice, FeePayment
from holidays.models import Holiday
from messaging.models import SMSMessage
from students.models import Enrollment, Student

from .forms import SchoolForm, SMSSettingsForm
from .mixins import ERPPermissionMixin
from .models import AuditLog


@login_required
def dashboard(request):
    from core.access import students_for, is_manager
    from examinations.models import ResultSnapshot
    from django.db.models import F
    from django.utils import timezone
    school=request.school
    if school is None:
        return render(request,"core/no_school.html")
    if not school.is_active:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    stats=[]
    if request.user.has_perm("students.view_student"):
        stats.append(("Students",students_for(request.user,school).count()))
    if request.user.has_perm("employees.view_employee"):
        stats.append(("Employees",Employee.objects.filter(school=school,status="active").count()))
    if request.user.has_perm("fees.view_feepayment"):
        stats.append(("Collected this month",FeePayment.objects.filter(school=school,is_cancelled=False,date__gte=timezone.localdate().replace(day=1)).aggregate(total=Sum("amount"))["total"] or 0))
    my_students=students_for(request.user,school) if not (is_manager(request.user) or request.user.has_perm("students.view_student")) else []
    snapshots=ResultSnapshot.objects.filter(school=school,enrollment__student__in=my_students,exam__status="published",version=F("exam__publication_version")).select_related("exam","enrollment")
    return render(request,"core/home.html",{"page_title":"Dashboard","stats":stats,"my_students":my_students,"snapshots":snapshots,
                  "holidays":Holiday.objects.filter(school=school,end_date__gte=timezone.localdate())[:8]})


class SchoolSettingsView(ERPPermissionMixin, UpdateView):
    permission_required = "core.change_school"
    form_class = SchoolForm
    template_name = "core/settings_school.html"

    def get_object(self, queryset=None):
        if self.request.school is None:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("Attach your account to a school first.")
        return self.request.school

    def form_valid(self, form):
        messages.success(self.request, "School settings saved.")
        return super().form_valid(form)

    def get_success_url(self):
        return self.request.path


class SMSSettingsView(SchoolSettingsView):
    form_class = SMSSettingsForm
    template_name = "core/settings_sms.html"


class AuditLogListView(ERPPermissionMixin, ListView):
    permission_required = "core.view_auditlog"
    template_name = "core/audit_log.html"
    paginate_by = 50

    def get_queryset(self):
        return AuditLog.objects.filter(school=self.request.school).select_related("user")


@login_required
def switch_year(request, pk):
    """Set the current academic year (Basic settings)."""
    if not request.user.has_perm("academics.change_academicyear"):
        messages.error(request, "Not allowed.")
        return redirect("dashboard")
    year = AcademicYear.objects.filter(school=request.school, pk=pk).first()
    if year:
        year.is_current = True
        year.save()
        messages.success(request, f"{year} is now the current academic year.")
    return redirect("settings:year_list")
