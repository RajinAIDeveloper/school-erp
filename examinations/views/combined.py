"""
Combined results, such as an annual result weighted from several exams.
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.access import is_manager, require_permission
from core.exports import spreadsheet

from ..documents import card_rows
from ..grading import headline
from ..models import Exam, GradeScale
from ..rulebooks import official_notice
from .common import _as_exam, card_enrollment, mask_name


@require_permission("examinations.view_combinedresult")
def combined_list(request):
    """Combined results, and a form to set one up from the year's exams with weights."""
    from academics.models import AcademicYear

    from ..combined import save_combined
    from ..models import CombinedResult

    years = AcademicYear.objects.filter(school=request.school)
    raw_year = request.POST.get("academic_year") or request.GET.get("year") or ""
    year = (
        years.filter(pk=int(raw_year)).first() if str(raw_year).isdigit() else AcademicYear.current_for(request.school)
    )
    exams = (
        Exam.objects.filter(school=request.school, academic_year=year).order_by("start_date", "name") if year else []
    )
    typed = {}
    if request.method == "POST":
        typed = {key: request.POST.get(key, "") for key in request.POST}
        try:
            scale = get_object_or_404(GradeScale, school=request.school, pk=request.POST.get("grade_scale") or 0)
            parts = []
            for exam in exams:
                raw = request.POST.get(f"weight-{exam.pk}", "").strip()
                if raw:
                    try:
                        parts.append((exam, Decimal(raw)))
                    except InvalidOperation:
                        raise ValidationError(f"{exam.name}: '{raw}' is not a number.") from None
            combined = save_combined(
                user=request.user,
                school=request.school,
                academic_year=year,
                name=request.POST.get("name", ""),
                grade_scale=scale,
                parts=parts,
            )
            messages.success(request, f"Set up {combined.name}.")
            return redirect("examinations:combined_detail", pk=combined.pk)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return render(
        request,
        "examinations/combined_list.html",
        {
            "combined": CombinedResult.objects.filter(school=request.school).select_related("academic_year"),
            "years": years,
            "year": year,
            "exams": [(exam, typed.get(f"weight-{exam.pk}", "")) for exam in exams],
            "scales": GradeScale.objects.filter(school=request.school),
            "typed": typed,
            "page_title": "Combined results",
        },
    )


@require_permission("examinations.view_combinedresult")
def combined_detail(request, pk):
    from ..combined import class_levels, combined_sheet, out_of_date, publish_combined
    from ..models import CombinedResult

    combined = get_object_or_404(
        CombinedResult.objects.select_related("academic_year", "grade_scale"), school=request.school, pk=pk
    )
    if request.method == "POST":
        try:
            publish_combined(combined, request.user)
            messages.success(request, f"Published {combined.name}.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("examinations:combined_detail", pk=combined.pk)
    levels = list(class_levels(combined))
    raw = request.GET.get("class_level", "")
    level = next((lv for lv in levels if str(lv.pk) == raw), levels[0] if levels else None)
    rows, problem = [], ""
    if level is not None:
        try:
            rows = combined_sheet(combined, level)
        except ValidationError as exc:
            problem = " ".join(exc.messages)
    book_label = rows[0]["rulebook"] if rows else ""
    if request.GET.get("format") == "csv" and rows:
        headers = ["Section", "Roll", "Student", "Total", "Percent", "GPA", "Overall"]
        body = [
            [r["section"], r["roll"], r["student"], r["total"], r["percent"] or "", r["gpa"] or "", headline(r)]
            for r in rows
        ]
        preamble = [
            ("Combined result", str(combined)),
            ("Class", str(level)),
            (
                "Version",
                f"Published version {combined.publication_version}"
                if combined.publication_version
                else "Draft: not published",
            ),
            ("Made from", ", ".join(f"{p.exam.name} {p.weight}%" for p in combined.parts.select_related("exam"))),
            ("Rulebook", book_label),
        ]
        return spreadsheet("combined-result", headers, body, "csv", preamble=preamble)
    return render(
        request,
        "examinations/combined_detail.html",
        {
            "combined": combined,
            "parts": combined.parts.select_related("exam"),
            "levels": levels,
            "level": level,
            "rows": rows,
            "problem": problem,
            "stale": out_of_date(combined),
            "can_publish": is_manager(request.user) and request.user.has_perm("examinations.change_combinedresult"),
            "page_title": str(combined),
        },
    )


@require_permission(None, also="managers; teachers of the section that year; the child's own family")
def combined_card(request, pk, student_pk):
    """A student's combined result card, from the published version, with the same access rules as an exam card."""
    from core.qr import qr_svg

    from ..combined import combined_sheet, out_of_date
    from ..documents import shows_points
    from ..models import CombinedResult, CombinedSnapshot

    combined = get_object_or_404(CombinedResult, school=request.school, pk=pk)
    exam_like = _as_exam(combined)
    student, enr = card_enrollment(request, exam_like, student_pk)
    if combined.status != "published" and not request.user.has_perm("examinations.view_mark"):
        raise PermissionDenied
    row = next((r for r in combined_sheet(combined, enr.class_level) if r["enrollment_id"] == enr.pk), None)
    if row is None:
        from django.http import Http404

        raise Http404
    snap = CombinedSnapshot.objects.filter(
        combined=combined, enrollment=enr, version=combined.publication_version
    ).first()
    link = (
        request.build_absolute_uri(reverse("examinations:combined_verify", args=[snap.verification_code]))
        if snap
        else ""
    )
    if request.GET.get("format") == "pdf":
        from ..documents import report_card_pdf

        return report_card_pdf(request.school, exam_like, enr, row, snap, verify_url=link)
    lines = card_rows(row)
    staff = request.user.has_perm("examinations.view_mark")
    return render(
        request,
        "examinations/report_card.html",
        {
            "row": row,
            "exam": exam_like,
            "snapshot": snap,
            "lines": lines,
            "show_parts": False,
            "show_points": shows_points(row),
            "attendance": row.get("attendance"),
            "attendance_until": None,
            "show_effort": False,
            "comment_span": 4 + shows_points(row),
            "effort_label": "Effort",
            "official_notice": official_notice(row),
            "board": row.get("system") == "national",
            "trace": row.get("trace") if staff else None,
            "stale": out_of_date(combined) if staff else [],
            "verify_url": link,
            "qr": qr_svg(link) if link else "",
            "page_title": "Combined result",
        },
    )


def combined_verify(request, code):
    """Public check of a combined result card, showing no more than the exam card's check."""
    from ..models import CombinedSnapshot

    snapshot = get_object_or_404(
        CombinedSnapshot.objects.select_related("combined__academic_year", "school"), verification_code=code
    )
    combined = snapshot.combined
    payload = snapshot.payload or {}
    return render(
        request,
        "examinations/verify.html",
        {
            "snapshot": snapshot,
            "school": snapshot.school,
            "exam": _as_exam(combined),
            "current": combined.status == "published" and snapshot.version == combined.publication_version,
            "initials": mask_name(payload.get("student", "")),
            "class_name": payload.get("section", ""),
            "roll": payload.get("roll", ""),
            "gpa": payload.get("gpa"),
            "result": payload.get("result", ""),
            "headline": headline(payload),
            "fingerprint": payload.get("fingerprint", ""),
            "page_title": "Report verification",
        },
    )
