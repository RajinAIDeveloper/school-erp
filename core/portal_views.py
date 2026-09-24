from datetime import date

from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext

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
            "pay_online": request.school.payment_gateway != "none",
            "page_title": gettext("My school records"),
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
            "page_title": gettext("My attendance"),
        },
    )


@require_permission(None)
def fees(request):
    students, student = select_student(request)
    if not request.GET.get("student") and len(students) > 1:
        # Open on the child who owes, not simply the first child on the list.
        owing = set(
            FeeInvoice.objects.filter(school=request.school, student__in=students)
            .outstanding()
            .values_list("student_id", flat=True)
        )
        student = next((s for s in students if s.pk in owing), student)
    invoices = (
        FeeInvoice.objects.filter(school=request.school, student=student).order_by("-issue_date") if student else []
    )
    return render(
        request,
        "portal/fees.html",
        {
            "students": students,
            "selected": student,
            "invoices": invoices,
            "pay_online": request.school.payment_gateway != "none",
            "page_title": gettext("My fees"),
        },
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
    from examinations.official import official_results_for

    return render(
        request,
        "portal/results.html",
        {
            "students": students,
            "selected": student,
            "snapshots": snapshots,
            # Only results a second person has checked against the body's statement.
            "official_results": official_results_for(student, confirmed_only=True) if student else [],
            "combined": _published_combined(student),
            "page_title": gettext("My results"),
        },
    )


def _published_combined(student):
    """The current published combined results (such as an annual result) for one child."""
    if student is None:
        return []
    from examinations.models import CombinedSnapshot

    return list(
        CombinedSnapshot.objects.filter(
            enrollment__student=student,
            combined__status="published",
            version=F("combined__publication_version"),
        ).select_related("combined")
    )


@require_permission(None)
def privacy(request):
    """Families see, give and withdraw their consent for each use of their child's data."""
    from django.contrib import messages
    from django.core.exceptions import PermissionDenied, ValidationError
    from django.shortcuts import redirect

    from students.models import GuardianConsent
    from students.privacy import current_consents, record_consent

    students, student = select_student(request)
    if request.method == "POST" and student is not None:
        try:
            record_consent(
                user=request.user,
                student=student,
                purpose=request.POST.get("purpose", ""),
                given=request.POST.get("given") == "1",
                method=GuardianConsent.Method.PORTAL,
            )
            messages.success(request, gettext("Your choice has been recorded."))
        except (ValidationError, PermissionDenied) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect(f"{request.path}?student={student.pk}")
    consents = current_consents(student) if student else {}
    rows = [(value, label, consents.get(value)) for value, label in GuardianConsent.Purpose.choices]
    return render(
        request,
        "portal/privacy.html",
        {
            "students": students,
            "selected": student,
            "rows": rows,
            "is_guardian": hasattr(request.user, "guardian_profile"),
            "page_title": gettext("Privacy and consent"),
        },
    )
