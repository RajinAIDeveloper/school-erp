from datetime import date

from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import render

from attendance.models import StudentAttendance
from core.access import require_permission, students_for
from core.exports import spreadsheet
from examinations.models import ResultSnapshot
from fees.models import FeeInvoice


def select_student(request):
    from django.core.exceptions import PermissionDenied

    if not (hasattr(request.user, "student_profile") or hasattr(request.user, "guardian_profile")):
        raise PermissionDenied("This account has no linked student or guardian profile.")
    students = list(students_for(request.user, request.school).order_by("student_id"))
    raw = request.GET.get("student")
    if raw:
        student = next((s for s in students if str(s.pk) == raw), None)
        if student is None:
            raise Http404
    else:
        student = students[0] if students else None
    return students, student


@require_permission(None)
def index(request):
    students, _ = select_student(request)
    from decimal import Decimal

    from django.db.models import Sum

    owed = {}
    if students:
        rows = (
            FeeInvoice.objects.filter(school=request.school, student__in=students)
            .outstanding()
            .values("student")
            .annotate(total=Sum("balance_amount"), count=Count("id"))
        )
        owed = {row["student"]: row for row in rows}
    cards = [
        {
            "student": student,
            "owed": owed.get(student.pk, {}).get("total") or Decimal("0.00"),
            "invoices": owed.get(student.pk, {}).get("count", 0),
        }
        for student in students
    ]
    family_total = sum((card["owed"] for card in cards), start=Decimal("0.00"))
    return render(
        request,
        "portal/index.html",
        {
            "students": students,
            "cards": cards,
            "family_total": family_total,
            "family_invoices": sum(card["invoices"] for card in cards),
            "page_title": "My school records",
        },
    )


@require_permission(None)
def attendance(request):
    students, student = select_student(request)
    raw = request.GET.get("month") or date.today().strftime("%Y-%m")
    try:
        year, month = map(int, raw.split("-"))
        if year < 1900 or month not in range(1, 13):
            raise ValueError
    except (ValueError, TypeError):
        year, month = date.today().year, date.today().month
    records = (
        list(
            StudentAttendance.objects.filter(
                school=request.school, enrollment__student=student, date__year=year, date__month=month
            ).order_by("-date")
        )
        if student
        else []
    )
    if request.GET.get("format") == "csv":
        return spreadsheet(
            "my-attendance",
            ["Student", "Date", "Status", "Remarks"],
            [[student.full_name, str(r.date), r.status, r.remarks] for r in records] if student else [],
        )
    return render(
        request,
        "portal/attendance.html",
        {
            "students": students,
            "selected": student,
            "records": records,
            "month": f"{year:04d}-{month:02d}",
            "page_title": "My attendance",
        },
    )


@require_permission(None)
def fees(request):
    students, student = select_student(request)
    invoices = (
        FeeInvoice.objects.filter(school=request.school, student=student).order_by("-issue_date") if student else []
    )
    return render(
        request,
        "portal/fees.html",
        {"students": students, "selected": student, "invoices": invoices, "page_title": "My fees"},
    )


@require_permission(None)
def results(request):
    students, student = select_student(request)
    snapshots = (
        ResultSnapshot.objects.filter(
            school=request.school,
            enrollment__student=student,
            exam__status="published",
            version=F("exam__publication_version"),
        ).select_related("exam")
        if student
        else []
    )
    return render(
        request,
        "portal/results.html",
        {"students": students, "selected": student, "snapshots": snapshots, "page_title": "My results"},
    )
