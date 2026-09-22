from datetime import date
from decimal import Decimal

from django import forms
from django.db.models import Count, Q, Sum
from django.shortcuts import render

from attendance.models import StaffAttendance, StudentAttendance
from core.access import require_permission
from core.exports import pdf_response, spreadsheet
from core.forms import TailwindFormMixin
from employees.models import Employee
from examinations.models import Exam
from fees.models import FeeInvoice, FeePayment
from finance.models import Account
from holidays.models import Holiday
from students.models import Student

REPORTS = [
    ("Students export", "students:export", "students.view_student", "CSV / Excel roster"),
    (
        "Student attendance",
        "attendance:student_report",
        "attendance.view_studentattendance",
        "Monthly grid and CSV / Excel",
    ),
    ("Staff attendance", "attendance:staff_report", "attendance.view_staffattendance", "Monthly grid and CSV / Excel"),
    ("Fee collections", "fees:report", "fees.view_feepayment", "Receipts by date with CSV / Excel"),
    ("Account balances", "finance:report", "finance.view_account", "Income, expenses and balances"),
    (
        "Results and subject analysis",
        "examinations:results",
        "examinations.view_mark",
        "Class and section rank, CSV / Excel / PDF",
    ),
    ("Class routine", "timetable:routine", "timetable.view_routineslot", "Routine CSV / Excel / PDF"),
    ("Holiday calendar", "holidays:calendar", "holidays.view_holiday", "iCalendar"),
]


@require_permission(None)
def hub(request):
    from django.urls import reverse

    links = [
        {"name": name, "url": reverse(route), "description": description}
        for name, route, permission, description in REPORTS
        if request.user.has_perm(permission)
    ]
    if request.user.has_perm("finance.view_account"):
        links.insert(
            0,
            {
                "name": "Management overview",
                "url": reverse("reports:overview"),
                "description": "Date-range counts, attendance, fees and accounts.",
            },
        )
    if hasattr(request.user, "student_profile") or hasattr(request.user, "guardian_profile"):
        links = [
            {
                "name": "My attendance",
                "url": reverse("portal:attendance"),
                "description": "Attendance history by month with CSV export.",
            },
            {
                "name": "My fees",
                "url": reverse("portal:fees"),
                "description": "Invoices, outstanding balances and receipts.",
            },
            {
                "name": "My results",
                "url": reverse("portal:results"),
                "description": "Published report cards for linked students.",
            },
            *links,
        ]
    return render(request, "reports/hub.html", {"links": links, "page_title": "Reports"})


class Filter(TailwindFormMixin, forms.Form):
    start = forms.DateField(initial=lambda: date.today().replace(day=1))
    end = forms.DateField(initial=date.today)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            self.add_error("end", "End date must follow start date.")
        return data


@require_permission("finance.view_account")
def overview(request):
    form = Filter(request.GET or None)
    rows = []
    if form.is_bound and form.is_valid():
        start, end = form.cleaned_data["start"], form.cleaned_data["end"]
        school = request.school
        student_counts = Student.objects.filter(school=school).aggregate(
            total=Count("id"), active=Count("id", filter=Q(status="active"))
        )
        staff_counts = Employee.objects.filter(school=school).aggregate(
            total=Count("id"), active=Count("id", filter=Q(status="active"))
        )
        st = StudentAttendance.objects.filter(school=school, date__range=(start, end)).aggregate(
            total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
        )
        emp = StaffAttendance.objects.filter(school=school, date__range=(start, end)).aggregate(
            total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
        )
        payments = FeePayment.objects.filter(school=school, date__range=(start, end), is_cancelled=False).aggregate(
            total=Sum("amount"), count=Count("id")
        )
        dues = sum(
            (i.balance for i in FeeInvoice.objects.filter(school=school, status__in=["unpaid", "partial"])),
            Decimal("0"),
        )
        income = sum(
            (a.balance(start, end) for a in Account.objects.filter(school=school, account_type="income")), Decimal("0")
        )
        expenses = sum(
            (a.balance(start, end) for a in Account.objects.filter(school=school, account_type="expense")), Decimal("0")
        )
        rows = [
            ["Active students", student_counts["active"]],
            ["Total students", student_counts["total"]],
            ["Active employees", staff_counts["active"]],
            ["Total employees", staff_counts["total"]],
            ["Student attendance recorded", st["total"]],
            ["Student attendance present or late", st["present"]],
            ["Staff attendance recorded", emp["total"]],
            ["Staff attendance present or late", emp["present"]],
            ["Fee payments in range", payments["count"]],
            ["Fee collected in range", payments["total"] or 0],
            ["Outstanding fees (as of now)", dues],
            ["Ledger income in range", income],
            ["Ledger expenses in range", expenses],
            ["Ledger net in range", income - expenses],
            ["Results published to date", Exam.objects.filter(school=school, status="published").count()],
            [
                "Holidays starting in range",
                Holiday.objects.filter(school=school, start_date__range=(start, end)).count(),
            ],
        ]
        fmt = request.GET.get("format")
        if fmt == "pdf":
            return pdf_response("School overview", ["Metric", "Value"], rows, f"{school.name} | {start} to {end}")
        if fmt in ("csv", "xlsx"):
            return spreadsheet("school-overview", ["Metric", "Value"], rows, fmt)
    return render(request, "reports/overview.html", {"form": form, "rows": rows, "page_title": "Management overview"})
