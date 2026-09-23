"""
The reports hub.

Every report a role may open, in one place, and each one exportable the same way. The
queries live in the module that owns the data, so a report never invents its own version
of a number that a screen elsewhere already answers.
"""

from datetime import date
from decimal import Decimal

from django import forms
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.urls import reverse

from core.access import is_manager, plans_timetable, require_permission
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.pdf import table_document

ZERO = Decimal("0.00")


def holds(permission):
    """A report gated by one Django permission."""

    def check(user):
        return user.has_perm(permission)

    return check


def timetable_planner(user):
    """A planning report: heads and whoever edits the routine, never a family."""
    return user.has_perm("timetable.view_routineslot") and plans_timetable(user)


# Reports that live in the module owning their data, linked rather than duplicated.
LINKED_REPORTS = [
    ("Students", "students:export", "students.view_student", "The roster with class, guardian and contact."),
    (
        "Student attendance",
        "attendance:student_report",
        "attendance.view_studentattendance",
        "A month per section, with working days and percentages.",
    ),
    (
        "Attendance today",
        "attendance:daily_summary",
        "attendance.view_studentattendance",
        "Which registers are taken, and who is present in each.",
    ),
    (
        "Staff attendance",
        "attendance:staff_report",
        "attendance.view_staffattendance",
        "The same month grid for teachers and staff.",
    ),
    ("Fee collections", "fees:report", "fees.view_feepayment", "Receipts by date, with method and student."),
    (
        "Outstanding fees",
        "fees:dues",
        "fees.view_feeinvoice",
        "Who owes what, by class, with days overdue and guardian phone.",
    ),
    ("Trial balance", "finance:trial_balance", "finance.view_account", "Every account's debit and credit."),
    (
        "Income and expenditure",
        "finance:income_statement",
        "finance.view_account",
        "What the school earned and spent in a period.",
    ),
    ("Balance sheet", "finance:balance_sheet", "finance.view_account", "Assets against liabilities and funds."),
    ("Cash book", "finance:cash_book", "finance.view_account", "Money in and out, day by day."),
    ("Ledger integrity", "finance:integrity", "finance.view_account", "Whether the books still balance."),
    (
        "Results and subject analysis",
        "examinations:results",
        "examinations.view_mark",
        "Class and section ranks with per-subject statistics.",
    ),
    (
        "Class routine",
        "timetable:routine",
        holds("timetable.view_routineslot"),
        "The week as a grid, per class or teacher.",
    ),
    ("Routine utilisation", "timetable:utilisation", timetable_planner, "Room use and periods per teacher."),
    ("Holiday calendar", "holidays:calendar", "holidays.view_holiday", "The school calendar as iCalendar."),
]

# Reports built here, because they cut across modules.
HUB_REPORTS = [
    (
        "Management overview",
        "reports:overview",
        "finance.view_account",
        "Counts, attendance, collections and ledger totals for a date range.",
    ),
    (
        "Student strength",
        "reports:strength",
        "students.view_student",
        "Students by class, section, gender and religion.",
    ),
    (
        "Payroll register",
        "reports:payroll_register",
        "finance.view_payroll",
        "A month's salaries, what was paid and what is outstanding.",
    ),
    (
        "Leave register",
        "reports:leave_register",
        "attendance.view_leaverequest",
        "Requests, decisions and days taken per employee.",
    ),
    (
        "Teacher load",
        "reports:teacher_load",
        timetable_planner,
        "Periods per week per teacher, heaviest first.",
    ),
]


class RangeForm(TailwindFormMixin, forms.Form):
    start = forms.DateField(initial=lambda: date.today().replace(day=1))
    end = forms.DateField(initial=date.today)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            self.add_error("end", "End date must follow start date.")
        return data

    def range(self):
        if self.is_bound and self.is_valid():
            return self.cleaned_data["start"], self.cleaned_data["end"]
        today = date.today()
        return today.replace(day=1), today


class MonthForm(TailwindFormMixin, forms.Form):
    month = forms.DateField(
        initial=lambda: date.today().replace(day=1),
        help_text="Any date in the month; the first is used.",
    )

    def month_start(self):
        if self.is_bound and self.is_valid():
            return self.cleaned_data["month"].replace(day=1)
        return date.today().replace(day=1)


def _export(request, title, headers, rows, filename, align_right=()):
    fmt = request.GET.get("format")
    if fmt == "pdf":
        return table_document(request.school, title, headers, rows, filename=filename, align_right=align_right)
    if fmt in ("csv", "xlsx"):
        return spreadsheet(filename.replace(".pdf", ""), headers, rows, fmt)
    return None


@require_permission(None)
def hub(request):
    """Every report this role may open, grouped so the list is readable."""
    links = []
    for name, route, rule, description in HUB_REPORTS + LINKED_REPORTS:
        # The same predicate the destination view uses, so the hub never offers a 403.
        allowed = rule(request.user) if callable(rule) else request.user.has_perm(rule)
        if allowed:
            links.append({"name": name, "url": reverse(route), "description": description})

    personal = []
    if hasattr(request.user, "student_profile") or hasattr(request.user, "guardian_profile"):
        personal = [
            {
                "name": "My attendance",
                "url": reverse("portal:attendance"),
                "description": "Attendance by month, with a CSV to keep.",
            },
            {"name": "My fees", "url": reverse("portal:fees"), "description": "Invoices, balances and receipts."},
            {
                "name": "My results",
                "url": reverse("portal:results"),
                "description": "Published report cards for each linked student.",
            },
        ]
    return render(
        request,
        "reports/hub.html",
        {"links": links, "personal": personal, "page_title": "Reports"},
    )


@require_permission("finance.view_account")
def overview(request):
    """One page a head can read before a meeting."""
    from attendance.models import StaffAttendance, StudentAttendance
    from employees.models import Employee
    from examinations.models import Exam
    from fees.models import FeeInvoice, FeePayment
    from finance import reports as ledger
    from holidays.models import Holiday
    from holidays.services import working_days
    from students.models import Student

    form = RangeForm(request.GET or None)
    start, end = form.range()
    school = request.school

    students = Student.objects.filter(school=school).aggregate(
        total=Count("id"), active=Count("id", filter=Q(status="active"))
    )
    staff = Employee.objects.filter(school=school).aggregate(
        total=Count("id"), active=Count("id", filter=Q(status="active"))
    )
    student_attendance = StudentAttendance.objects.filter(school=school, date__range=(start, end)).aggregate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    staff_attendance = StaffAttendance.objects.filter(school=school, date__range=(start, end)).aggregate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    payments = FeePayment.objects.filter(school=school, date__range=(start, end), is_cancelled=False).aggregate(
        total=Sum("amount"), count=Count("id")
    )
    dues = (
        FeeInvoice.objects.filter(school=school).outstanding().aggregate(total=Sum("balance_amount"), count=Count("id"))
    )
    income = ledger.income_statement(school, start, end)

    rows = [
        ["Working days in range", working_days(school, start, end)],
        ["Active students", students["active"]],
        ["Students on file", students["total"]],
        ["Active employees", staff["active"]],
        ["Employees on file", staff["total"]],
        ["Student attendance recorded", student_attendance["total"]],
        ["Student attendance present or late", student_attendance["present"]],
        [
            "Student attendance rate among recorded rows",
            f"{round(student_attendance['present'] * 100 / student_attendance['total'])}%"
            if student_attendance["total"]
            else "—",
        ],
        ["Staff attendance recorded", staff_attendance["total"]],
        ["Staff attendance present or late", staff_attendance["present"]],
        [
            "Staff attendance rate among recorded rows",
            f"{round(staff_attendance['present'] * 100 / staff_attendance['total'])}%"
            if staff_attendance["total"]
            else "—",
        ],
        ["Fee receipts in range", payments["count"]],
        ["Fee collected in range", payments["total"] or ZERO],
        ["Outstanding invoices now", dues["count"] or 0],
        ["Outstanding amount now", dues["total"] or ZERO],
        ["Ledger income in range", income["total_income"]],
        ["Ledger expenditure in range", income["total_expense"]],
        ["Ledger surplus in range", income["surplus"]],
        ["Exams published to date", Exam.objects.filter(school=school, status="published").count()],
        ["Holidays starting in range", Holiday.objects.filter(school=school, start_date__range=(start, end)).count()],
    ]
    exported = _export(request, "School overview", ["Measure", "Value"], rows, "school-overview.pdf", align_right=(1,))
    return exported or render(
        request,
        "reports/overview.html",
        {"form": form, "rows": rows, "start": start, "end": end, "page_title": "Management overview"},
    )


@require_permission("students.view_student")
def strength(request):
    """How many students there are, and how they are distributed."""
    from academics.models import AcademicYear
    from students.models import Enrollment, Student

    year = AcademicYear.current_for(request.school)
    by_class = list(
        Enrollment.objects.filter(
            school=request.school,
            academic_year=year,
            status=Enrollment.Status.ENROLLED,
            student__status="active",
        )
        .values("section__class_level__name", "section__name")
        .annotate(
            total=Count("id"),
            boys=Count("id", filter=Q(student__gender="M")),
            girls=Count("id", filter=Q(student__gender="F")),
        )
        .order_by("section__class_level__order", "section__name")
        if year
        else []
    )
    headers = ["Class", "Section", "Boys", "Girls", "Total"]
    rows = [
        [row["section__class_level__name"], row["section__name"], row["boys"], row["girls"], row["total"]]
        for row in by_class
    ]
    rows.append(
        [
            "All",
            "",
            sum(r["boys"] for r in by_class),
            sum(r["girls"] for r in by_class),
            sum(r["total"] for r in by_class),
        ]
    )
    religion = list(
        Student.objects.filter(school=request.school, status="active")
        .values("religion")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    exported = _export(request, "Student strength", headers, rows, "student-strength.pdf", align_right=(2, 3, 4))
    return exported or render(
        request,
        "reports/strength.html",
        {
            "headers": headers,
            "rows": rows,
            "religion": religion,
            "year": year,
            "page_title": "Student strength",
        },
    )


@require_permission("finance.view_payroll")
def payroll_register(request):
    """A month's salaries, and what of it is still unpaid."""
    from finance.models import Payroll

    form = MonthForm(request.GET or None)
    month = form.month_start()
    rows_qs = (
        Payroll.objects.filter(school=request.school, month=month)
        .select_related("employee__designation")
        .order_by("employee__employee_id")
    )
    headers = ["Employee", "Designation", "Basic", "Allowance", "Deduction", "Net", "Paid on"]
    rows = [
        [
            row.employee.full_name,
            str(row.employee.designation or ""),
            row.basic,
            row.allowance,
            row.deduction,
            row.net,
            row.paid_on or "Not paid",
        ]
        for row in rows_qs
    ]
    total = sum((row.net for row in rows_qs), start=ZERO)
    unpaid = sum((row.net for row in rows_qs if not row.paid_on), start=ZERO)
    if rows:
        rows.append(["Total", "", "", "", "", total, ""])
    exported = _export(
        request,
        f"Payroll register - {month:%B %Y}",
        headers,
        rows,
        "payroll-register.pdf",
        align_right=(2, 3, 4, 5),
    )
    return exported or render(
        request,
        "reports/table.html",
        {
            "form": form,
            "headers": headers,
            "rows": rows,
            "summary": [("Total net pay", total, True), ("Still unpaid", unpaid, True)],
            "page_title": f"Payroll register · {month:%B %Y}",
        },
    )


@require_permission("attendance.view_leaverequest", also="own requests unless a manager")
def leave_register(request):
    """Who asked for leave, what was decided, and how many days it came to."""
    from attendance.models import LeaveRequest

    form = RangeForm(request.GET or None)
    start, end = form.range()
    requests = (
        LeaveRequest.objects.filter(school=request.school, start_date__lte=end, end_date__gte=start)
        .select_related("employee", "leave_type", "reviewed_by")
        .order_by("employee__employee_id", "start_date")
    )
    if not is_manager(request.user):
        requests = requests.filter(employee__user=request.user)
    headers = ["Employee", "Type", "From", "To", "Days", "Status", "Decided by"]
    rows = [
        [
            row.employee.full_name,
            str(row.leave_type),
            row.start_date,
            row.end_date,
            row.days,
            row.get_status_display(),
            str(row.reviewed_by or ""),
        ]
        for row in requests
    ]
    approved = sum(row.days for row in requests if row.status == "approved")
    exported = _export(request, "Leave register", headers, rows, "leave-register.pdf", align_right=(4,))
    return exported or render(
        request,
        "reports/table.html",
        {
            "form": form,
            "headers": headers,
            "rows": rows,
            "summary": [("Requests", len(rows), False), ("Approved days", approved, False)],
            "page_title": "Leave register",
        },
    )


@require_permission("timetable.view_routineslot", also="timetable editors only")
def teacher_load(request):
    """Periods a week per teacher, heaviest first."""
    from django.core.exceptions import PermissionDenied

    from academics.models import AcademicYear
    from timetable.services import teacher_load as load_rows

    if not plans_timetable(request.user):
        raise PermissionDenied("This report is for staff who plan the timetable.")

    year = AcademicYear.current_for(request.school)
    rows_data = load_rows(request.school, year) if year else []
    headers = ["Teacher", "Periods per week"]
    rows = [[row["teacher"].full_name, row["periods"]] for row in rows_data]
    exported = _export(request, "Teacher load", headers, rows, "teacher-load.pdf", align_right=(1,))
    return exported or render(
        request,
        "reports/table.html",
        {
            "headers": headers,
            "rows": rows,
            "summary": [
                ("Teachers", len(rows), False),
                ("Periods assigned", sum(row["periods"] for row in rows_data), False),
            ],
            "page_title": "Teacher load",
        },
    )
