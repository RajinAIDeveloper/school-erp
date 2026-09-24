"""
What each role sees when they sign in.

A dashboard is only worth the screen space if it answers the question that role actually
arrives with. A teacher wants to know whose register is still missing; an accountant
wants today's takings and who owes; a head wants to know whether the school is running.
So each role gets its own panel rather than one page of averages.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

ZERO = Decimal("0.00")


def _current_year(school):
    from academics.models import AcademicYear

    return AcademicYear.current_for(school)


def manager_dashboard(school, user):
    """For an administrator or head: is the school running today?"""
    from attendance.models import LeaveRequest, StaffAttendance, StudentAttendance
    from employees.models import Employee
    from examinations.models import UnlockRequest
    from fees.models import FeeInvoice, FeePayment
    from holidays.models import Holiday, is_holiday
    from students.models import Enrollment, Student

    year = _current_year(school)

    today = timezone.localdate()
    month_start = today.replace(day=1)

    closed_today = is_holiday(school, today)
    attendance = StudentAttendance.objects.filter(school=school, date=today).aggregate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    # The denominator is the school's roll, not the rows that happen to exist. One child
    # marked present in a class of thirty is 3%% attendance with 29 registers still to take,
    # and reporting it as 100%% is how a missing register goes unnoticed for a week.
    expected_today = (
        Enrollment.objects.filter(
            school=school,
            academic_year=year,
            status=Enrollment.Status.ENROLLED,
            student__status="active",
        ).count()
        if year
        else 0
    )
    outstanding = (
        FeeInvoice.objects.filter(school=school).outstanding().aggregate(total=Sum("balance_amount"), count=Count("id"))
    )
    collected = (
        FeePayment.objects.filter(school=school, is_cancelled=False, date__gte=month_start).aggregate(
            total=Sum("amount")
        )["total"]
        or ZERO
    )

    strength = (
        Enrollment.objects.filter(school=school, academic_year=year)
        .values("class_level__name", "class_level__order")
        .annotate(count=Count("id"))
        .order_by("class_level__order")
        if year
        else []
    )
    enrolled = sum(row["count"] for row in strength)

    return {
        "kind": "manager",
        "closed_today": closed_today,
        "registers_outstanding": max(expected_today - attendance["total"], 0) if not closed_today else 0,
        "tiles": [
            {
                "label": "Active students",
                "value": Student.objects.filter(school=school, status="active").count(),
                "note": f"{enrolled} enrolled in {year}" if year else "No academic year set",
            },
            {
                "label": "Teachers and staff",
                "value": Employee.objects.filter(school=school, status="active").count(),
                "note": f"{StaffAttendance.objects.filter(school=school, date=today, status__in=['present', 'late']).count()} in today",
            },
            {
                "label": "Attendance today",
                "value": (
                    f"{round(attendance['present'] * 100 / expected_today)}%"
                    if expected_today and attendance["total"]
                    else ("Closed" if closed_today else "Not taken")
                ),
                "note": (
                    "the school is closed"
                    if closed_today
                    else f"{attendance['total']} of {expected_today} on the roll recorded"
                ),
                "tone": "muted" if closed_today else _attendance_tone(attendance, expected_today),
            },
            {
                "label": "Collected this month",
                "value": collected,
                "money": True,
                "note": f"{outstanding['count'] or 0} invoice(s) still owing",
            },
        ],
        "strength": list(strength),
        "enrolled": enrolled,
        "outstanding": outstanding["total"] or ZERO,
        "approvals": {
            "leaves": LeaveRequest.objects.filter(school=school, status="pending").count(),
            "unlocks": UnlockRequest.objects.filter(school=school, status="pending").count(),
        },
        "recent_payments": FeePayment.objects.filter(school=school, is_cancelled=False)
        .select_related("invoice__student")
        .order_by("-date", "-id")[:6],
        "recent_students": Student.objects.filter(school=school).order_by("-created_at")[:5],
        "upcoming_holidays": Holiday.objects.filter(school=school, end_date__gte=today).order_by("start_date")[:5],
        "gender_split": list(
            Student.objects.filter(school=school, status="active")
            .values("gender")
            .annotate(count=Count("id"))
            .order_by("gender")
        ),
    }


def _attendance_tone(attendance, expected=None):
    """Colour the tile by the share of the people it should cover, not of those recorded."""
    denominator = expected if expected is not None else attendance["total"]
    if not denominator or not attendance["total"]:
        return "muted"
    percent = attendance["present"] * 100 / denominator
    return "good" if percent >= 90 else "warn" if percent >= 75 else "bad"


def teacher_dashboard(school, user):
    """For a teacher: what is still waiting for me today?"""
    from attendance.models import StudentAttendance
    from attendance.services import register_state
    from core.access import sections_for
    from examinations.models import ExamSchedule, Mark
    from holidays.models import is_holiday
    from students.models import Enrollment
    from timetable.models import RoutineSlot

    today = timezone.localdate()
    year = _current_year(school)
    sections = list(sections_for(user, school).select_related("class_level"))
    employee = getattr(user, "employee_profile", None)
    closed_today = is_holiday(school, today)

    # Two counts per section rather than a flag: a register with one row out of thirty is
    # not "taken", and a teacher who was called away mid-roll needs to see that.
    recorded = dict(
        StudentAttendance.objects.filter(school=school, date=today, enrollment__section__in=sections)
        .values_list("enrollment__section")
        .annotate(n=Count("id"))
    )
    roll = dict(
        Enrollment.objects.filter(
            school=school,
            section__in=sections,
            academic_year=year,
            status=Enrollment.Status.ENROLLED,
            student__status="active",
        )
        .values_list("section")
        .annotate(n=Count("id"))
    )
    registers = []
    if not closed_today:
        for section in sections:
            expected = roll.get(section.pk, 0)
            if not expected:
                continue
            done = recorded.get(section.pk, 0)
            registers.append(
                {
                    "section": section,
                    "state": register_state(done, expected),
                    "taken": register_state(done, expected) == "complete",
                    "recorded": done,
                    "expected": expected,
                }
            )

    today_slots = (
        RoutineSlot.objects.filter(school=school, academic_year=year, weekday=today.isoweekday(), teacher=employee)
        .select_related("subject", "section__class_level", "period", "room")
        .order_by("period__order")
        if employee and year
        else []
    )

    pending_marks = []
    if employee and year:
        assignments = employee.subject_assignments.filter(academic_year=year).select_related(
            "subject", "section__class_level"
        )
        for assignment in assignments:
            schedules = (
                ExamSchedule.objects.filter(
                    school=school,
                    subject=assignment.subject,
                    class_level=assignment.section.class_level,
                    exam__academic_year=year,
                )
                .exclude(exam__status="published")
                .select_related("exam")
            )
            for schedule in schedules:
                # Only the students who sit this paper: a Humanities pupil is not "missing" Physics.
                from examinations.services import expected_marks

                expected = expected_marks(schedule, section=assignment.section)
                entered = Mark.objects.filter(schedule=schedule, enrollment__section=assignment.section).count()
                if expected and entered < expected:
                    pending_marks.append(
                        {
                            "schedule": schedule,
                            "section": assignment.section,
                            "entered": entered,
                            "expected": expected,
                        }
                    )

    from django.utils.translation import gettext

    complete = sum(1 for row in registers if row["state"] == "complete")
    return {
        "kind": "teacher",
        "closed_today": closed_today,
        "tiles": [
            {"label": gettext("My sections"), "value": len(sections), "note": gettext("assigned to you")},
            {
                "label": gettext("Registers today"),
                "value": gettext("Closed") if closed_today else f"{complete}/{len(registers)}",
                "note": (
                    gettext("the school is closed today")
                    if closed_today
                    else (gettext("complete") if registers else gettext("nothing to take"))
                ),
                "tone": (
                    "muted"
                    if closed_today
                    else ("warn" if any(r["state"] != "complete" for r in registers) else "good")
                ),
            },
            {
                "label": gettext("Papers awaiting marks"),
                "value": len(pending_marks),
                "note": gettext("unpublished exams"),
                "tone": "warn" if pending_marks else "good",
            },
            {"label": gettext("Periods today"), "value": len(today_slots), "note": gettext("on your routine")},
        ],
        "registers": registers,
        "today_slots": today_slots,
        "pending_marks": pending_marks[:8],
    }


def accountant_dashboard(school, user):
    """For an accountant: what came in, and what is still out?"""
    from fees.models import FeeInvoice, FeePayment
    from finance import reports

    today = timezone.localdate()
    month_start = today.replace(day=1)
    week_end = today + timedelta(days=7)

    by_method = list(
        FeePayment.objects.filter(school=school, is_cancelled=False, date__gte=month_start)
        .values("method")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    outstanding = FeeInvoice.objects.filter(school=school).outstanding()
    defaulters = list(
        outstanding.select_related("student", "enrollment__section__class_level").order_by("-balance_amount")[:8]
    )
    today_total = (
        FeePayment.objects.filter(school=school, is_cancelled=False, date=today).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )
    income = reports.income_statement(school, month_start, today)

    return {
        "kind": "accountant",
        "tiles": [
            {"label": "Collected today", "value": today_total, "money": True, "note": f"{today:%d %b}"},
            {
                "label": "Collected this month",
                "value": sum((row["total"] for row in by_method), start=ZERO),
                "money": True,
                "note": f"{sum(row['count'] for row in by_method)} receipt(s)",
            },
            {
                "label": "Outstanding",
                "value": outstanding.aggregate(total=Sum("balance_amount"))["total"] or ZERO,
                "money": True,
                "note": f"{outstanding.count()} invoice(s)",
                "tone": "warn",
            },
            {
                "label": "Surplus this month",
                "value": income["surplus"],
                "money": True,
                "note": "income less expenditure",
                "tone": "bad" if income["surplus"] < ZERO else "good",
            },
        ],
        "by_method": by_method,
        "defaulters": defaulters,
        "due_this_week": outstanding.filter(due_date__range=(today, week_end)).count(),
        "income": income,
    }


def staff_dashboard(school, user):
    """For a staff member: my own attendance, leave and payslips."""
    from attendance.models import LeaveRequest, StaffAttendance
    from attendance.services import leave_balance

    today = timezone.localdate()
    month_start = today.replace(day=1)
    employee = getattr(user, "employee_profile", None)
    if employee is None:
        return {"kind": "staff", "tiles": [], "unlinked": True}

    from holidays.services import working_days

    attendance = StaffAttendance.objects.filter(employee=employee, date__gte=month_start).aggregate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    # Against the days the school was actually open, from whichever came later: the start
    # of the month or the day this person joined.
    expected = working_days(school, max(month_start, employee.joining_date), today)
    balances = leave_balance(employee, today.year)
    return {
        "kind": "staff",
        "employee": employee,
        "tiles": [
            {
                "label": "Attendance this month",
                "value": (f"{round(attendance['present'] * 100 / expected)}%" if expected else "—"),
                "note": f"{attendance['present']} of {expected} working day(s)",
                "tone": _attendance_tone(attendance, expected),
            },
            {
                "label": "Leave remaining",
                "value": sum(row["remaining"] for row in balances),
                "note": "days across all types",
            },
            {
                "label": "Leave requests",
                "value": LeaveRequest.objects.filter(employee=employee).count(),
                "note": f"{LeaveRequest.objects.filter(employee=employee, status='pending').count()} pending",
            },
        ],
        "balances": balances,
        "recent_attendance": StaffAttendance.objects.filter(employee=employee).order_by("-date")[:14],
        "leaves": LeaveRequest.objects.filter(employee=employee).select_related("leave_type")[:5],
        "payrolls": employee.payrolls.order_by("-month")[:6] if user.has_perm("finance.view_payroll") else [],
    }


def build(school, user):
    """Pick the panel that answers this person's question."""
    from core.roles import ACCOUNTANT, MANAGERS, TEACHER, has_role

    if has_role(user, *MANAGERS):
        return manager_dashboard(school, user)
    if has_role(user, ACCOUNTANT):
        return accountant_dashboard(school, user)
    if has_role(user, TEACHER):
        return teacher_dashboard(school, user)
    return staff_dashboard(school, user)
