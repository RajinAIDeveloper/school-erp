from datetime import date, datetime

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import is_manager, require_permission, sections_for, students_for
from core.exports import spreadsheet
from core.forms import SchoolModelForm, TailwindFormMixin
from core.models import audit
from core.security import safe_next
from employees.models import Employee

from .models import AttendanceStatus, LeaveRequest, StaffAttendance, StudentAttendance
from .services import (
    apply_leave,
    enrollments_for,
    leave_balance,
    leave_days_in_year,
    monthly_matrix,
    overlapping_leave,
    register_state,
    save_register,
    self_check,
    withdraw_leave,
)


def _parse_date(raw):
    """A YYYY-MM-DD query parameter, or None when it is missing or malformed."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _month_anchor(raw):
    """First day of the requested YYYY-MM, falling back to the current month."""
    try:
        year, month = (int(part) for part in str(raw).split("-")[:2])
        if 1900 <= year <= 2999 and 1 <= month <= 12:
            return date(year, month, 1)
    except (TypeError, ValueError):
        pass
    return timezone.localdate().replace(day=1)


class RegisterFilter(TailwindFormMixin, forms.Form):
    date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    section = forms.ModelChoiceField(queryset=None, required=False)

    def __init__(self, *args, user, school, staff=False, taking=False, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import register_sections

        self.fields["section"].queryset = register_sections(user, school) if taking else sections_for(user, school)
        if staff:
            self.fields.pop("section")
        else:
            self.fields["section"].required = True


def _filter_data(request, staff, sections):
    """
    What the register filter is bound to, and the initial values when it is left unbound.

    Opening the screen, or following a link that carries only a date, picks the section when
    the person has exactly one. With several to choose from the form waits for a choice
    rather than greeting the teacher with an error they did not cause.
    """
    if request.method == "POST":
        return request.POST, {}
    data = request.GET.copy()
    data.setdefault("date", timezone.localdate().isoformat())
    if staff or data.get("section"):
        return data, {}
    only = list(sections.values_list("pk", flat=True)[:2])
    if len(only) == 1:
        data["section"] = str(only[0])
        return data, {}
    return None, {"date": data["date"]}


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
    from .services import register_sections

    data, initial = _filter_data(request, staff, register_sections(request.user, request.school))
    form = RegisterFilter(data, initial=initial, user=request.user, school=request.school, staff=staff, taking=True)
    rows = []
    closed = False
    if form.is_valid():
        day = form.cleaned_data["date"]
        from .services import register_closed

        closed = register_closed(request.user, request.school, day)
        if closed:
            editable = False
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
            "closed_days": request.school.register_edit_days if closed else 0,
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
    data, initial = _filter_data(request, staff, sections_for(request.user, request.school))
    form = RegisterFilter(data, initial=initial, user=request.user, school=request.school, staff=staff)
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

    def clean(self):
        """
        Two rules a school actually cares about: nobody books the same day twice, and
        nobody books past their entitlement without someone deciding to allow it.
        """
        data = super().clean()
        employee = data.get("employee")
        leave_type = data.get("leave_type")
        start, end = data.get("start_date"), data.get("end_date")
        if not (employee and start and end):
            return data
        if end < start:
            self.add_error("end_date", "The last day cannot be before the first.")
            return data
        clash = overlapping_leave(employee, start, end, exclude_pk=self.instance.pk).first()
        if clash:
            self.add_error(
                None,
                f"{employee.full_name} already has leave from {clash.start_date:%d %b} "
                f"to {clash.end_date:%d %b} ({clash.get_status_display().lower()}).",
            )
        school = employee.school
        if leave_days_in_year(school, start, end, start.year) + leave_days_in_year(school, start, end, end.year) == 0:
            self.add_error("start_date", "The request covers no working days: every day in it is a weekend or holiday.")
            return data
        if leave_type and not leave_type.allow_negative:
            # A request that straddles New Year is charged to each year for the days in it.
            for year in sorted({start.year, end.year}):
                requested = leave_days_in_year(school, start, end, year)
                balance = next((row for row in leave_balance(employee, year) if row["leave_type"] == leave_type), None)
                if balance and requested > balance["remaining"]:
                    self.add_error(
                        "leave_type",
                        f"{balance['remaining']} working day(s) of {leave_type} remain in {year}, "
                        f"but {requested} were asked for.",
                    )
        return data


@require_permission("attendance.view_studentattendance", also="own students only")
def student_history(request, pk):
    """One student's month, for the office and for the family that asks about it."""
    from students.models import Enrollment

    student = get_object_or_404(students_for(request.user, request.school), pk=pk)
    day = _month_anchor(request.GET.get("month"))
    enrollments = list(
        Enrollment.objects.filter(school=request.school, student=student)
        .select_related("academic_year", "section__class_level")
        .filter(academic_year__start_date__lte=day, academic_year__end_date__gte=day)
    )
    days, rows = monthly_matrix(request.school, enrollments, day.year, day.month)
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet(
            f"attendance-{student.student_id}",
            ["Name", *[str(d.day) for d in days], "Present", "Working days", "Percent"],
            [[r["name"], *r["cells"], r["present"], r["working"], r["pct"]] for r in rows],
            request.GET["format"],
        )
    return render(
        request,
        "attendance/student_history.html",
        {
            "student": student,
            "days": days,
            "rows": rows,
            "month": f"{day.year:04d}-{day.month:02d}",
            "page_title": f"Attendance · {student.full_name}",
        },
    )


@require_permission("attendance.view_studentattendance", also="own sections only")
def daily_summary(request):
    """Which sections have been marked today, and how many were present in each."""
    day = _parse_date(request.GET.get("date")) or timezone.localdate()
    sections = sections_for(request.user, request.school).select_related("class_level")
    records = (
        StudentAttendance.objects.filter(school=request.school, date=day, enrollment__section__in=sections)
        .values("enrollment__section")
        .annotate(
            total=Count("id"),
            present=Count("id", filter=Q(status__in=["present", "late"])),
            absent=Count("id", filter=Q(status="absent")),
            leave=Count("id", filter=Q(status="leave")),
        )
    )
    by_section = {row["enrollment__section"]: row for row in records}
    # Who took each register, and when it was last saved: the people named, not just a count.
    takers = {}
    for section_id, first, last, username, saved in (
        StudentAttendance.objects.filter(school=request.school, date=day, enrollment__section__in=sections)
        .values_list(
            "enrollment__section",
            "recorded_by__first_name",
            "recorded_by__last_name",
            "recorded_by__username",
            "updated_at",
        )
        .order_by("updated_at")
    ):
        names, latest = takers.get(section_id, ([], None))
        name = f"{first} {last}".strip() or username or "Unknown"
        if name not in names:
            names.append(name)
        takers[section_id] = (names, saved)
    from academics.models import AcademicYear
    from students.models import Enrollment

    year = AcademicYear.current_for(request.school)
    rolls = dict(
        Enrollment.objects.filter(
            school=request.school,
            section__in=sections,
            academic_year=year,
            status=Enrollment.Status.ENROLLED,
            student__status="active",
        )
        .values_list("section")
        .annotate(n=Count("id"))
    )
    labels = {"complete": "Complete", "partial": "Partial", "missing": "Not taken"}
    rows = []
    for section in sections:
        row = by_section.get(section.pk)
        recorded = row["total"] if row else 0
        expected = rolls.get(section.pk, 0)
        state = register_state(recorded, expected)
        rows.append(
            [
                str(section),
                expected,
                recorded,
                row["present"] if row else 0,
                row["absent"] if row else 0,
                row["leave"] if row else 0,
                f"{labels[state]} ({recorded} of {expected})" if state == "partial" else labels[state],
                ", ".join(takers[section.pk][0]) if section.pk in takers else "",
                timezone.localtime(takers[section.pk][1]).strftime("%H:%M") if section.pk in takers else "",
            ]
        )
    headers = ["Section", "On roll", "Recorded", "Present", "Absent", "On leave", "Register", "Taken by", "Last saved"]
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet(f"attendance-summary-{day}", headers, rows, request.GET["format"])
    return render(
        request,
        "generic/report.html",
        {
            "page_title": f"Attendance summary · {day:%d %b %Y}",
            "headers": headers,
            "rows": rows,
            "form": None,
            "intro": (
                "Sections marked 'Not taken' still need a register for this date; 'Partial' means fewer "
                "rows were recorded than there are active students on the roll."
            ),
        },
    )


@require_permission("attendance.view_leaverequest", also="own requests unless a manager")
def leaves(request):
    """Managers see the school's requests; everyone else sees their own."""
    import calendar

    qs = LeaveRequest.objects.filter(school=request.school).select_related("employee", "leave_type", "reviewed_by")
    mine = getattr(request.user, "employee_profile", None)
    can_review = is_manager(request.user) and request.user.has_perm("attendance.change_leaverequest")
    if not can_review:
        qs = qs.filter(employee__user=request.user)
    status = request.GET.get("status", "")
    if status in dict(LeaveRequest.Status.choices):
        qs = qs.filter(status=status)
    employee = request.GET.get("employee", "")
    if can_review and employee.isdigit():
        qs = qs.filter(employee_id=int(employee))
    month = request.GET.get("month", "")
    if month:
        anchor = _month_anchor(month)
        last = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
        qs = qs.filter(start_date__lte=last, end_date__gte=anchor)
        month = f"{anchor.year:04d}-{anchor.month:02d}"
    return render(
        request,
        "attendance/leaves.html",
        {
            "rows": qs,
            "page_title": "Leave requests",
            "can_review": can_review,
            "status": status,
            "statuses": LeaveRequest.Status.choices,
            "employee": employee,
            "employees": (
                Employee.objects.filter(school=request.school).order_by("first_name", "last_name")
                if can_review
                else Employee.objects.none()
            ),
            "month": month,
            "pending_count": LeaveRequest.objects.filter(school=request.school, status="pending").count(),
            "balances": leave_balance(mine, timezone.localdate().year) if mine else [],
        },
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


@require_permission("attendance.change_leaverequest", also="managers only")
@require_POST
def leave_review(request, pk):
    if not is_manager(request.user):
        raise PermissionDenied
    obj = get_object_or_404(LeaveRequest, school=request.school, pk=pk)
    status = request.POST.get("status")
    if status not in ("approved", "rejected"):
        messages.error(request, "Choose approve or reject.")
        return redirect("attendance:leave_list")
    # The decision and its register rows land together or not at all.
    with transaction.atomic():
        obj = LeaveRequest.objects.select_for_update().get(pk=obj.pk)
        was_approved = obj.status == LeaveRequest.Status.APPROVED
        obj.status = status
        obj.reviewed_by = request.user
        obj.reviewed_at = timezone.now()
        obj.review_note = request.POST.get("note", "")[:200]
        obj.save()
        # An approval marks those days as leave on the register; reversing a decision
        # removes only the rows it created, never a manual entry.
        if status == LeaveRequest.Status.APPROVED:
            outcome = apply_leave(obj, request.user)
            (messages.warning if outcome.skipped else messages.success)(request, f"Approved. {outcome.summary}")
        else:
            if was_approved:
                withdraw_leave(obj, request.user)
            messages.success(request, "Request rejected.")
        audit(request, "leave." + status, obj, obj.review_note)
    return redirect("attendance:leave_list")


@require_permission(None)
@require_POST
def check_in(request):
    """Staff record their own arrival, then departure, when the school allows it."""
    employee = getattr(request.user, "employee_profile", None)
    if employee is None:
        raise Http404("Your account is not linked to an employee record.")
    try:
        row = self_check(employee, request.user)
        if row.check_out:
            messages.success(request, f"Checked out at {row.check_out:%H:%M}.")
        else:
            messages.success(request, f"Checked in at {row.check_in:%H:%M}.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(safe_next(request, request.POST.get("next"), "attendance:staff_take"))
