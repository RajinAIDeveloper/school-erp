"""
Exam series, candidate entries and official results.

This is the exam officer's record of what the school entered with Cambridge, Pearson, the IB
or an education board, and of what the body awarded. It is kept entirely apart from the
school's own assessments: an internal grade is never shown as official, and an official
result is only ever what the body's statement of results says.

The system keeps these records; entries are submitted through the body's own portal.
"""

import csv
from io import StringIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school, is_manager
from core.models import AuditLog

from .models import OfficialResult, SeriesCandidate, SeriesEntry

RESULT_COLUMNS = ("candidate_number", "syllabus_code", "grade")
OPTIONAL_COLUMNS = ("syllabus_title", "points", "kind")
MAX_RESULT_ROWS = 5000


def _allowed(user, school, permission):
    assert_actor_school(user, school)
    if not user.has_perm(permission):
        raise PermissionDenied


def _audit(series, user, action, description):
    AuditLog.objects.create(school=series.school, user=user, action=action, description=f"{series}: {description}")


# ------------------------------------------------------------------ candidates and entries


def _next_number(series, start=None):
    numbers = [int(n) for n in series.candidates.values_list("candidate_number", flat=True) if n.isdigit()]
    return max(numbers + [(start or 1) - 1]) + 1


@transaction.atomic
def add_candidates(*, user, series, students, first_number=None):
    """
    Register students as candidates for a series, numbering them on from the highest number
    already used (four digits, as Cambridge and Pearson print them). Students already
    registered are left as they are.
    """
    _allowed(user, series.school, "examinations.add_seriescandidate")
    assert_school(series.school, *students)
    existing = set(series.candidates.values_list("student_id", flat=True))
    number = _next_number(series, first_number)
    made = []
    for student in students:
        if student.pk in existing:
            continue
        made.append(
            SeriesCandidate(school=series.school, series=series, student=student, candidate_number=f"{number:04d}")
        )
        number += 1
    SeriesCandidate.objects.bulk_create(made)
    if made:
        _audit(series, user, "series.candidates_added", f"{len(made)} candidate(s)")
    return len(made)


@transaction.atomic
def update_candidate(*, user, candidate, candidate_number, uci="", certificate_name=None):
    _allowed(user, candidate.school, "examinations.change_seriescandidate")
    number, uci = (candidate_number or "").strip(), (uci or "").strip()
    if certificate_name is not None:
        name = certificate_name.strip()
        if len(name) > 60:
            raise ValidationError("The name on the certificate can be at most 60 characters.")
        candidate.certificate_name = name
    if not number or len(number) > 12:
        raise ValidationError("Give the candidate number the body issued, up to 12 characters.")
    if (
        SeriesCandidate.objects.filter(series=candidate.series, candidate_number=number)
        .exclude(pk=candidate.pk)
        .exists()
    ):
        raise ValidationError(f"Candidate number {number} is already used in this series.")
    candidate.candidate_number, candidate.uci = number, uci[:20]
    candidate.save(update_fields=["candidate_number", "uci", "certificate_name", "updated_at"])
    return candidate


@transaction.atomic
def add_entries(
    *,
    user,
    series,
    candidates,
    qualification,
    syllabus_code,
    syllabus_title,
    option_code="",
    tier="",
    level="",
    subject=None,
):
    """Enter candidates for one syllabus. Candidates already entered for it are reported, not duplicated."""
    _allowed(user, series.school, "examinations.add_seriesentry")
    qualification, code = (qualification or "").strip(), (syllabus_code or "").strip().upper()
    title = (syllabus_title or "").strip()
    if not qualification or not code or not title:
        raise ValidationError("Give the qualification, the syllabus code and its title.")
    if tier not in SeriesEntry.Tier.values:
        raise ValidationError("Choose a tier from the list.")
    if level not in ("", "HL", "SL"):
        raise ValidationError("Choose HL, SL or neither.")
    if subject is not None:
        assert_school(series.school, subject)
    for candidate in candidates:
        if candidate.series_id != series.pk:
            raise ValidationError("A candidate in this submission belongs to another series.")
    already = set(
        SeriesEntry.objects.filter(candidate__in=candidates, syllabus_code=code).values_list("candidate_id", flat=True)
    )
    made = [
        SeriesEntry(
            school=series.school,
            candidate=candidate,
            subject=subject,
            qualification=qualification,
            syllabus_code=code,
            syllabus_title=title,
            option_code=(option_code or "").strip().upper(),
            tier=tier,
            level=level,
        )
        for candidate in candidates
        if candidate.pk not in already
    ]
    SeriesEntry.objects.bulk_create(made)
    if made:
        _audit(series, user, "series.entries_added", f"{code} for {len(made)} candidate(s)")
    return len(made), len(already)


@transaction.atomic
def withdraw_entry(*, user, entry):
    _allowed(user, entry.school, "examinations.change_seriesentry")
    entry = SeriesEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.status == SeriesEntry.Status.WITHDRAWN:
        raise ValidationError("This entry is already withdrawn.")
    entry.status, entry.withdrawn_at = SeriesEntry.Status.WITHDRAWN, timezone.now()
    entry.save(update_fields=["status", "withdrawn_at", "updated_at"])
    _audit(entry.candidate.series, user, "series.entry_withdrawn", f"{entry}")
    return entry


# ------------------------------------------------------------------ official results


def read_result_rows(upload):
    if upload.size > 2 * 1024 * 1024:
        raise ValidationError("The file is larger than 2 MB.")
    try:
        rows = list(csv.DictReader(StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValidationError("Upload a UTF-8 CSV file.") from exc
    if not rows or len(rows) > MAX_RESULT_ROWS:
        raise ValidationError(f"The file needs between 1 and {MAX_RESULT_ROWS} rows.")
    missing = [c for c in RESULT_COLUMNS if c not in rows[0]]
    if missing:
        raise ValidationError(f"The file needs the columns {', '.join(RESULT_COLUMNS)}; missing {', '.join(missing)}.")
    return [{k: (row.get(k) or "").strip() for k in RESULT_COLUMNS + OPTIONAL_COLUMNS} for row in rows]


def check_results(series, rows):
    """
    What a results file will do, without writing: each row matched to a candidate by number,
    never by name. Returns (prepared, errors). A row that repeats the current result is
    marked unchanged; one that differs is an amendment and will need a reason.
    """
    candidates = {c.candidate_number: c for c in series.candidates.select_related("student")}
    entries = {(e.candidate_id, e.syllabus_code): e for e in SeriesEntry.objects.filter(candidate__series=series)}
    current = {
        (r.candidate_id, r.syllabus_code, r.kind): r
        for r in OfficialResult.objects.filter(candidate__series=series, is_current=True)
    }
    prepared, errors, seen = [], [], set()
    for number, row in enumerate(rows, start=2):
        candidate = candidates.get(row["candidate_number"])
        code = row["syllabus_code"].upper()
        kind = row.get("kind") or OfficialResult.Kind.SUBJECT
        if candidate is None:
            errors.append(f"Row {number}: no candidate {row['candidate_number'] or '(blank)'} in this series.")
            continue
        if not code or not row["grade"]:
            errors.append(f"Row {number}: a syllabus code and a grade are needed.")
            continue
        if len(row["grade"]) > 10 or len(row.get("points", "")) > 10:
            errors.append(f"Row {number}: the grade and points are at most 10 characters.")
            continue
        if kind not in OfficialResult.Kind.values:
            errors.append(f"Row {number}: kind must be subject, unit or overall.")
            continue
        key = (candidate.pk, code, kind)
        if key in seen:
            errors.append(f"Row {number}: {candidate.candidate_number} {code} appears twice in the file.")
            continue
        seen.add(key)
        entry = entries.get((candidate.pk, code))
        previous = current.get(key)
        if previous and (previous.grade, previous.points) == (row["grade"], row.get("points", "")):
            action = "unchanged"
        elif previous:
            action = "amend"
        else:
            action = "new"
        prepared.append(
            {
                "candidate_id": candidate.pk,
                "candidate_number": candidate.candidate_number,
                "student": candidate.student.full_name,
                "entry_id": entry.pk if entry else None,
                "entered": entry is not None,
                "syllabus_code": code,
                "syllabus_title": row.get("syllabus_title") or (entry.syllabus_title if entry else ""),
                "grade": row["grade"],
                "points": row.get("points", ""),
                "kind": kind,
                "action": action,
                "previous": previous.grade if previous else "",
            }
        )
    return prepared, errors


@transaction.atomic
def import_results(*, user, series, prepared, source, received_on, amendment_reason=""):
    """
    Record checked rows as the body's results. New results are added; amended ones supersede
    the current record, which is kept; unchanged rows are skipped.
    """
    _allowed(user, series.school, "examinations.add_officialresult")
    source, reason = (source or "").strip(), (amendment_reason or "").strip()
    if not source:
        raise ValidationError("Say where these results come from, e.g. the statement of results.")
    if received_on is None or received_on > timezone.localdate():
        raise ValidationError("Give the date the results were received; it cannot be in the future.")
    amending = [row for row in prepared if row["action"] == "amend"]
    if amending and not reason:
        raise ValidationError(f"{len(amending)} result(s) change an earlier one. Give the reason for the amendment.")
    added = amended = 0
    for row in prepared:
        if row["action"] == "unchanged":
            continue
        candidate = SeriesCandidate.objects.get(pk=row["candidate_id"], series=series)
        previous = None
        if row["action"] == "amend":
            previous = (
                OfficialResult.objects.select_for_update()
                .filter(candidate=candidate, syllabus_code=row["syllabus_code"], kind=row["kind"], is_current=True)
                .first()
            )
            if previous is not None:
                previous.is_current = False
                previous.save(update_fields=["is_current", "updated_at"])
        OfficialResult.objects.create(
            school=series.school,
            candidate=candidate,
            entry_id=row["entry_id"],
            kind=row["kind"],
            syllabus_code=row["syllabus_code"],
            syllabus_title=row["syllabus_title"][:100],
            grade=row["grade"],
            points=row["points"],
            source=source[:100],
            received_on=received_on,
            recorded_by=user,
            supersedes=previous,
            amendment_reason=reason[:200] if previous else "",
        )
        if previous:
            amended += 1
        else:
            added += 1
    _audit(series, user, "official_results.imported", f"{added} new, {amended} amended, from {source}")
    return added, amended


@transaction.atomic
def confirm_results(*, user, series):
    """
    A second person confirms the imported results against the body's statement. Until then
    they are visible to staff only. The person who imported them cannot confirm them.
    """
    _allowed(user, series.school, "examinations.change_officialresult")
    if not is_manager(user):
        raise PermissionDenied
    waiting = OfficialResult.objects.filter(candidate__series=series, is_current=True, checked_at__isnull=True)
    own = waiting.filter(recorded_by=user).count()
    count = waiting.exclude(recorded_by=user).update(checked_by=user, checked_at=timezone.now())
    if count:
        _audit(series, user, "official_results.confirmed", f"{count} result(s)")
    return count, own


def official_results_for(student, *, confirmed_only):
    """A student's current official results, newest series first."""
    results = (
        OfficialResult.objects.filter(candidate__student=student, is_current=True)
        .select_related("candidate__series", "supersedes")
        .order_by("-candidate__series__results_date", "candidate__series__name", "syllabus_code")
    )
    if confirmed_only:
        # Families see a result once it is confirmed and the body's release date has come.
        from django.db.models import Q

        today = timezone.localdate()
        results = results.filter(checked_at__isnull=False).filter(
            Q(candidate__series__results_date__isnull=True) | Q(candidate__series__results_date__lte=today)
        )
    return list(results)
