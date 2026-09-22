from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import is_manager, require_permission, sections_for
from core.exports import spreadsheet
from core.forms import SchoolModelForm, TailwindFormMixin
from core.models import audit
from employees.models import Employee

from .models import AttendanceStatus, LeaveRequest, StaffAttendance, StudentAttendance
from .services import enrollments_for, monthly_matrix, save_register


class RegisterFilter(TailwindFormMixin, forms.Form):
    date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    section = forms.ModelChoiceField(queryset=None, required=False)

    def __init__(self, *args, user, school, staff=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["section"].queryset = sections_for(user, school)
        if staff:
            self.fields.pop("section")
        else:
            self.fields["section"].required = True


class RowForm(forms.Form):
    status = forms.ChoiceField(choices=[("", "Not recorded"), *AttendanceStatus.choices], required=False)
    remarks = forms.CharField(max_length=200, required=False)
    check_in = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    check_out = forms.TimeField(required=False, widget=forms.TimeInput(attrs={"type": "time"}))


def register(request, staff=False):
    permission = "attendance.view_staffattendance" if staff else "attendance.view_studentattendance"
    if not request.user.has_perm(permission):
        raise PermissionDenied
    editable = request.user.has_perm(
        "attendance.change_staffattendance" if staff else "attendance.change_studentattendance"
    )
    data = request.POST if request.method == "POST" else request.GET or {"date": timezone.localdate()}
    form = RegisterFilter(data, user=request.user, school=request.school, staff=staff)
    rows = []
    if form.is_valid():
        day = form.cleaned_data["date"]
        if staff:
            objects = Employee.objects.filter(school=request.school, status__in=["active", "on_leave"])
            if not is_manager(request.user):
                objects = objects.filter(user=request.user)
            records = {r.employee_id: r for r in StaffAttendance.objects.filter(school=request.school, date=day)}
        else:
            objects = enrollments_for(form.cleaned_data["section"], day)
            records = {r.enrollment_id: r for r in StudentAttendance.objects.filter(school=request.school, date=day)}
        entries = []
        valid = True
        for obj in objects:
            record = records.get(obj.pk)
            initial = (
                {key: getattr(record, key, None) for key in ("status", "remarks", "check_in", "check_out")}
                if record
                else {}
            )
            row_form = RowForm(request.POST if request.method == "POST" else None, prefix=str(obj.pk), initial=initial)
            rows.append({"object": obj, "name": obj.full_name if staff else obj.student.full_name, "form": row_form})
            if request.method == "POST":
                if not row_form.is_valid():
                    valid = False
                elif row_form.cleaned_data["status"]:
                    d = row_form.cleaned_data
                    entries.append((obj, d["status"], d["remarks"], d["check_in"], d["check_out"]))
        if request.method == "POST" and valid:
            try:
                save_register(school=request.school, user=request.user, day=day, entries=entries, staff=staff)
                messages.success(request, "Attendance saved.")
                return redirect(request.get_full_path())
            except ValidationError as e:
                form.add_error(None, e)
    return render(
        request,
        "attendance/take.html",
        {
            "filter_form": form,
            "rows": rows,
            "staff": staff,
            "editable": editable,
            "page_title": "Staff attendance" if staff else "Student attendance",
        },
    )


@require_permission(None)
def student_take(request):
    return register(request)


@require_permission(None)
def staff_take(request):
    return register(request, True)


@require_permission(None)
def report(request, staff=False):
    permission = "attendance.view_staffattendance" if staff else "attendance.view_studentattendance"
    if not request.user.has_perm(permission):
        raise PermissionDenied
    form = RegisterFilter(
        request.GET or {"date": timezone.localdate()}, user=request.user, school=request.school, staff=staff
    )
    days, rows = [], []
    if form.is_valid():
        day = form.cleaned_data["date"]
        if staff:
            objects = Employee.objects.filter(school=request.school)
            if not is_manager(request.user):
                objects = objects.filter(user=request.user)
        else:
            section = form.cleaned_data["section"]
            import calendar

            from students.models import Enrollment

            month_start = day.replace(day=1)
            month_end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
            objects = Enrollment.objects.filter(
                school=request.school,
                section=section,
                academic_year__start_date__lte=month_end,
                academic_year__end_date__gte=month_start,
            ).select_related("student", "academic_year")
        days, rows = monthly_matrix(request.school, list(objects), day.year, day.month, staff)
        if request.GET.get("format") in ("csv", "xlsx"):
            return spreadsheet(
                "attendance",
                ["Name", *[str(d.day) for d in days], "Present", "Working days", "Percent"],
                [[r["name"], *r["cells"], r["present"], r["working"], r["pct"]] for r in rows],
                request.GET["format"],
            )
    return render(
        request,
        "attendance/report.html",
        {"filter_form": form, "rows": rows, "days": days, "page_title": "Monthly attendance report"},
    )


class LeaveForm(SchoolModelForm):
    class Meta:
        model = LeaveRequest
        fields = ["employee", "leave_type", "start_date", "end_date", "reason"]


@require_permission("attendance.view_leaverequest")
def leaves(request):
    qs = LeaveRequest.objects.filter(school=request.school).select_related("employee", "leave_type")
    if not is_manager(request.user):
        qs = qs.filter(employee__user=request.user)
    return render(
        request,
        "attendance/leaves.html",
        {"rows": qs, "page_title": "Leave requests", "can_review": is_manager(request.user)},
    )


@require_permission("attendance.add_leaverequest")
def leave_create(request):
    form = LeaveForm(request.POST or None, school=request.school)
    if not is_manager(request.user):
        form.fields["employee"].queryset = form.fields["employee"].queryset.filter(user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("attendance:leave_list")
    return render(request, "generic/form.html", {"form": form, "page_title": "Request leave"})


@require_permission("attendance.change_leaverequest")
@require_POST
def leave_review(request, pk):
    if not is_manager(request.user):
        raise PermissionDenied
    obj = get_object_or_404(LeaveRequest, school=request.school, pk=pk)
    status = request.POST.get("status")
    if status not in ("approved", "rejected"):
        raise ValidationError("Invalid review decision.")
    with transaction.atomic():
        obj.status = status
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.save()
        audit(request, "leave." + status, obj)
    return redirect("attendance:leave_list")
