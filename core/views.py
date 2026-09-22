from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import ListView, UpdateView

from academics.models import AcademicYear
from employees.models import Employee
from fees.models import FeePayment
from holidays.models import Holiday

from .access import require_permission
from .forms import SchoolForm, SMSSettingsForm
from .mixins import ERPPermissionMixin
from .models import AuditLog, audit


@login_required
def dashboard(request):
    from django.db.models import F
    from django.utils import timezone

    from core.access import is_manager, students_for
    from examinations.models import ResultSnapshot

    school = request.school
    if school is None:
        return render(request, "core/no_school.html")
    if not school.is_active:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    stats = []
    if request.user.has_perm("students.view_student"):
        stats.append(("Students", students_for(request.user, school).count()))
    if request.user.has_perm("employees.view_employee"):
        stats.append(("Employees", Employee.objects.filter(school=school, status="active").count()))
    if request.user.has_perm("fees.view_feepayment"):
        stats.append(
            (
                "Collected this month",
                FeePayment.objects.filter(
                    school=school, is_cancelled=False, date__gte=timezone.localdate().replace(day=1)
                ).aggregate(total=Sum("amount"))["total"]
                or 0,
            )
        )
    my_students = (
        students_for(request.user, school)
        if not (is_manager(request.user) or request.user.has_perm("students.view_student"))
        else []
    )
    snapshots = ResultSnapshot.objects.filter(
        school=school,
        enrollment__student__in=my_students,
        exam__status="published",
        version=F("exam__publication_version"),
    ).select_related("exam", "enrollment")
    return render(
        request,
        "core/home.html",
        {
            "page_title": "Dashboard",
            "stats": stats,
            "my_students": my_students,
            "snapshots": snapshots,
            "holidays": Holiday.objects.filter(school=school, end_date__gte=timezone.localdate())[:8],
        },
    )


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


@require_permission("academics.change_academicyear")
@require_POST
def switch_year(request, pk):
    """Make one academic year current. Every other year of the school is stood down."""
    year = get_object_or_404(AcademicYear, school=request.school, pk=pk)
    if not year.is_current:
        year.is_current = True
        year.save()
        audit(request, "academic_year.switched", year, f"{year} is now the current session.")
    messages.success(request, f"{year} is now the current academic year.")
    return redirect("settings:year_list")
