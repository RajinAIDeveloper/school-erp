"""
Awarding-body exam series: entries, official results and board registration.
"""

from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from academics.models import ClassLevel, Section
from core.access import is_manager, require_permission, sections_for
from core.exports import spreadsheet
from students.models import Enrollment

OFFICIAL_IMPORT_KEY = "official_results_import"


@require_permission("examinations.view_examseries")
def series_list(request):
    from ..models import ExamSeries

    series = ExamSeries.objects.filter(school=request.school).annotate(
        candidate_count=Count("candidates", distinct=True)
    )
    return render(request, "examinations/series_list.html", {"series": series, "page_title": "Exam series"})


def _series(request, pk):
    from ..models import ExamSeries

    return get_object_or_404(ExamSeries, school=request.school, pk=pk)


@require_permission("examinations.view_examseries")
def series_detail(request, pk):
    """
    Candidates and entries for one series: register a section's students, correct candidate
    numbers, enter candidates for a syllabus, and withdraw entries (which are kept).
    """
    from academics.models import Subject
    from students.models import Student

    from ..models import SeriesCandidate, SeriesEntry
    from ..official import add_candidates, add_entries, update_candidate, withdraw_entry

    series = _series(request, pk)
    here = reverse("examinations:series_detail", args=[series.pk])
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "add_candidates":
                section = get_object_or_404(Section, school=request.school, pk=request.POST.get("section") or 0)
                start = request.POST.get("first_number", "").strip()
                from academics.models import AcademicYear

                students = list(
                    Student.objects.filter(
                        school=request.school,
                        enrollments__section=section,
                        enrollments__academic_year=AcademicYear.current_for(request.school),
                        enrollments__status=Enrollment.Status.ENROLLED,
                    ).distinct()
                )
                added = add_candidates(
                    user=request.user,
                    series=series,
                    students=students,
                    first_number=int(start) if start.isdigit() else None,
                )
                messages.success(request, f"Registered {added} candidate(s) from {section}.")
            elif action == "update_candidate":
                candidate = get_object_or_404(SeriesCandidate, series=series, pk=request.POST.get("candidate") or 0)
                update_candidate(
                    user=request.user,
                    candidate=candidate,
                    candidate_number=request.POST.get("candidate_number", ""),
                    uci=request.POST.get("uci", ""),
                    certificate_name=request.POST.get("certificate_name"),
                )
                messages.success(request, f"Updated {candidate.student}.")
            elif action == "add_entries":
                chosen = [pk for pk in request.POST.getlist("candidates") if pk.isdigit()]
                candidates = list(SeriesCandidate.objects.filter(series=series, pk__in=chosen))
                if not candidates:
                    raise ValidationError("Tick the candidates to enter.")
                raw_subject = request.POST.get("subject", "")
                subject = (
                    get_object_or_404(Subject, school=request.school, pk=raw_subject) if raw_subject.isdigit() else None
                )
                made, skipped = add_entries(
                    user=request.user,
                    series=series,
                    candidates=candidates,
                    qualification=request.POST.get("qualification", ""),
                    syllabus_code=request.POST.get("syllabus_code", ""),
                    syllabus_title=request.POST.get("syllabus_title", ""),
                    option_code=request.POST.get("option_code", ""),
                    tier=request.POST.get("tier", ""),
                    level=request.POST.get("level", ""),
                    subject=subject,
                )
                note = f" ({skipped} already entered)" if skipped else ""
                messages.success(request, f"Entered {made} candidate(s){note}.")
            elif action == "arrangement":
                from ..candidates import save_arrangement
                from ..models import AccessArrangement

                candidate = get_object_or_404(SeriesCandidate, series=series, pk=request.POST.get("candidate") or 0)
                raw = request.POST.get("arrangement", "")
                existing = (
                    get_object_or_404(AccessArrangement, candidate__series=series, pk=raw) if raw.isdigit() else None
                )
                raw_consent = request.POST.get("consent_on", "")
                try:
                    consent = date.fromisoformat(raw_consent) if raw_consent else None
                except ValueError:
                    raise ValidationError("Enter the consent date as a date.") from None
                save_arrangement(
                    user=request.user,
                    candidate=candidate,
                    kind=request.POST.get("kind", ""),
                    details=request.POST.get("details", ""),
                    evidence=request.POST.get("evidence", ""),
                    consent_on=consent,
                    status=request.POST.get("status", "draft"),
                    arrangement=existing,
                )
                messages.success(request, f"Saved the access arrangement for {candidate.student}.")
            elif action == "withdraw":
                entry = get_object_or_404(SeriesEntry, candidate__series=series, pk=request.POST.get("entry") or 0)
                withdraw_entry(user=request.user, entry=entry)
                messages.success(request, f"Withdrew {entry.syllabus_code} for {entry.candidate.student}.")
            return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    candidates = series.candidates.select_related("student").prefetch_related("entries")
    from ..candidates import entry_problems
    from ..models import AccessArrangement

    sees_arrangements = is_manager(request.user) and request.user.has_perm("examinations.view_accessarrangement")
    if sees_arrangements:
        from students.privacy import log_sensitive

        log_sensitive(request, "Access arrangements", str(series))
    return render(
        request,
        "examinations/series_detail.html",
        {
            "series": series,
            "candidates": candidates,
            "problems": entry_problems(series),
            "arrangements": (
                AccessArrangement.objects.filter(candidate__series=series).select_related("candidate__student")
                if sees_arrangements
                else None
            ),
            "arrangement_kinds": AccessArrangement.Kind.choices,
            "arrangement_statuses": AccessArrangement.Status.choices,
            "sections": sections_for(request.user, request.school).select_related("class_level"),
            "subjects": Subject.objects.filter(school=request.school),
            "tiers": SeriesEntry.Tier.choices,
            "page_title": str(series),
        },
    )


@require_permission("examinations.view_officialresult")
def series_results(request, pk):
    """
    Official results for a series: import the body's statement of results as a CSV (checked
    and shown before anything is written), confirm them, and see amendments.
    """
    from datetime import date as _date

    from ..models import OfficialResult
    from ..official import check_results, confirm_results, import_results, read_result_rows

    series = _series(request, pk)
    here = reverse("examinations:series_results", args=[series.pk])
    preview = None
    session_key = f"{OFFICIAL_IMPORT_KEY}:{series.pk}"
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "check" and request.FILES.get("file"):
                if not request.user.has_perm("examinations.add_officialresult"):
                    raise PermissionDenied
                prepared, errors = check_results(series, read_result_rows(request.FILES["file"]))
                preview = {
                    "rows": prepared,
                    "errors": errors,
                    "new": sum(r["action"] == "new" for r in prepared),
                    "amend": sum(r["action"] == "amend" for r in prepared),
                    "unchanged": sum(r["action"] == "unchanged" for r in prepared),
                    "unentered": sum(not r["entered"] for r in prepared),
                }
                if errors:
                    request.session.pop(session_key, None)
                else:
                    request.session[session_key] = prepared
            elif action == "import":
                prepared = request.session.get(session_key)
                if not prepared:
                    raise ValidationError("Check a file first; nothing is waiting to be imported.")
                try:
                    received = _date.fromisoformat(request.POST.get("received_on", ""))
                except ValueError:
                    received = None
                added, amended = import_results(
                    user=request.user,
                    series=series,
                    prepared=prepared,
                    source=request.POST.get("source", ""),
                    received_on=received,
                    amendment_reason=request.POST.get("amendment_reason", ""),
                )
                request.session.pop(session_key, None)
                messages.success(
                    request, f"Recorded {added} new and {amended} amended result(s). Another manager must confirm them."
                )
                return redirect(here)
            elif action == "confirm":
                count, own = confirm_results(user=request.user, series=series)
                note = f" {own} you imported yourself need another manager to confirm." if own else ""
                messages.success(request, f"Confirmed {count} result(s).{note}")
                return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    results = (
        OfficialResult.objects.filter(candidate__series=series)
        .select_related("candidate__student", "recorded_by", "checked_by", "supersedes")
        .order_by("candidate__candidate_number", "syllabus_code", "-created_at")
    )
    waiting = results.filter(is_current=True, checked_at__isnull=True).count()
    return render(
        request,
        "examinations/series_results.html",
        {
            "series": series,
            "results": results,
            "preview": preview,
            "waiting": waiting,
            "today": timezone.localdate().isoformat(),
            "page_title": f"Official results · {series}",
        },
    )


@require_permission("examinations.view_examseries")
def series_export(request, pk):
    """The series' entries as a file for the exam officer to check and upload to the body's own system."""
    from ..candidates import entry_rows

    series = _series(request, pk)
    headers, rows = entry_rows(series)
    fmt = "xlsx" if request.GET.get("format") == "xlsx" else "csv"
    preamble = [
        ("Series", str(series)),
        ("Centre", series.centre_number or "not set"),
        ("Entries", str(len(rows))),
        ("Note", "Check against the body's entry system before uploading; this file is not sent anywhere."),
    ]
    return spreadsheet(f"entries-{series.pk}", headers, rows, fmt, preamble=preamble)


@require_permission("students.view_student", also="managers only: identity numbers of students and parents")
def board_registration(request):
    """
    The national board registration (eSIF) fields for a class, with the gaps to fill first.

    It carries birth registration and parents' NID numbers, so only the school's managers
    can open it.
    """
    from academics.models import AcademicYear

    from ..candidates import registration_rows

    if not is_manager(request.user):
        raise PermissionDenied("Only the school's managers can export registration data.")
    year = AcademicYear.current_for(request.school)
    levels = ClassLevel.objects.filter(school=request.school, is_active=True).order_by("order")
    raw = request.GET.get("class_level", "")
    level = levels.filter(pk=int(raw)).first() if raw.isdigit() else None
    headers, rows, checks = registration_rows(year, level) if (year and level) else ([], [], [])
    if level:
        from students.privacy import log_sensitive

        log_sensitive(
            request,
            "Board registration data",
            f"{level} ({request.GET.get('format') or 'screen'})",
        )
    if level and request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet(
            f"board-registration-{level.pk}",
            headers,
            rows,
            request.GET["format"],
            preamble=[
                ("Class", str(level)),
                ("Year", str(year)),
                ("Students", str(len(rows))),
                ("Gaps to fill", str(len(checks))),
            ],
        )
    return render(
        request,
        "examinations/board_registration.html",
        {
            "levels": levels,
            "level": level,
            "year": year,
            "headers": headers,
            "rows": rows,
            "checks": checks,
            "page_title": "Board registration data",
        },
    )
