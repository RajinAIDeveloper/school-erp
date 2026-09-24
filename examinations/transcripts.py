"""
Transcripts: a student's results across their years at the school, for a university or another
school.

A transcript reports grades exactly as the school awarded them, never converted, with the
grading key each was awarded under. It adds the official results an awarding body sent, once
confirmed and released, and the school's approved predicted grades for work still in progress.
It is issued once, numbered and frozen: a later correction changes nothing on it, and the school
reissues. The results it drew on are recorded, so the school and anyone checking it can see
when one of them has since been corrected.

Only these facts are copied. A result's frozen record also holds parents' names, comments,
ranks and working notes, which have no place on a transcript.
"""

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School

from .models import CombinedSnapshot, GradeForecast, OfficialResult, ResultSnapshot, Transcript, TranscriptRequest

PREFIX = "TR"
NOTES = [
    "Grades are shown exactly as the school awarded them, under the grading key printed with them. "
    "They have not been converted to any other scale.",
    "School results are the school's own assessments. Official results are issued only by the awarding "
    "body named against them.",
    "Predicted grades are the school's judgement of the grade a student is likely to achieve. They are not results.",
]


def _fingerprint(payload):
    from students.certificates import fingerprint

    return fingerprint(payload)


def _next_serial(school, year):
    prefix = f"{PREFIX}-{year}-"
    taken = Transcript.objects.filter(school=school, serial__startswith=prefix).values_list("serial", flat=True)
    highest = max((int(s.rsplit("-", 1)[1]) for s in taken if s.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{prefix}{highest + 1:04d}"


# ------------------------------------------------------------------ what each year can show


def _key(snapshot):
    kind = "combined" if isinstance(snapshot, CombinedSnapshot) else "exam"
    return f"{kind}:{snapshot.pk}"


def _label(snapshot):
    if isinstance(snapshot, CombinedSnapshot):
        return snapshot.combined.name
    return snapshot.exam.name


def candidate_years(student):
    """
    Each year the student has a published result in, oldest first, with the results that could
    stand for that year. The default is the year's latest combined result (an annual result),
    else its latest published exam.
    """
    from examinations.public import published_results

    exams, combined = published_results(student)
    years = {}
    for snapshot in [*combined, *exams]:
        enrollment = snapshot.enrollment
        year = enrollment.academic_year
        entry = years.setdefault(year.pk, {"year": year, "enrollment": enrollment, "choices": []})
        entry["choices"].append((_key(snapshot), _label(snapshot), snapshot))
    result = []
    for entry in sorted(years.values(), key=lambda item: item["year"].start_date):
        combined_choices = [c for c in entry["choices"] if c[0].startswith("combined:")]
        exam_choices = sorted(
            (c for c in entry["choices"] if c[0].startswith("exam:")),
            key=lambda c: (
                c[2].exam.end_date or c[2].exam.start_date or c[2].exam.created_at.date(),
                c[2].exam.published_at,
            ),
        )
        default = (combined_choices or exam_choices[::-1] or [None])[0]
        result.append({**entry, "default": default[0] if default else None})
    return result


# ------------------------------------------------------------------ the payload


def _subjects(payload, with_percent):
    rows = payload.get("subjects") or []
    if rows:
        return [
            {
                "name": row.get("name", ""),
                "code": ", ".join(row.get("codes") or [])
                if isinstance(row.get("codes"), list)
                else row.get("codes", ""),
                "letter": row.get("letter", ""),
                "grade_point": row.get("grade_point"),
                "percent": row.get("percent") if with_percent else None,
                "fourth": bool(row.get("is_fourth")),
                "level": row.get("level", ""),
            }
            for row in rows
            if not row.get("missing")
        ]
    # Older results kept no subject list: take the papers.
    return [
        {
            "name": cell.get("subject", ""),
            "code": cell.get("subject_code", ""),
            "letter": cell.get("letter", ""),
            "grade_point": cell.get("grade_point"),
            "percent": cell.get("percent") if with_percent else None,
            "fourth": bool(cell.get("is_fourth")),
            "level": cell.get("level", ""),
        }
        for cell in payload.get("cells") or []
        if not cell.get("missing")
    ]


def _keys(payloads):
    """The grading keys actually used, each once: the result's scale and any paper's own scale."""
    from .rulebooks import rulebook

    keys, seen = [], set()

    def add(label, name, rules):
        if not rules:
            return
        rows = [
            {
                "letter": r.get("letter", ""),
                "min": r.get("min_percent"),
                "max": r.get("max_percent"),
                "point": r.get("grade_point"),
            }
            for r in rules
        ]
        marker = (name, tuple((r["letter"], str(r["min"]), str(r["point"])) for r in rows))
        if marker in seen:
            return
        seen.add(marker)
        keys.append({"label": label, "scale": name, "rules": rows})

    for payload in payloads:
        policy = payload.get("policy") or {}
        label = rulebook(payload.get("system") or policy.get("rulebook") or "own").label
        add(label, policy.get("scale", ""), policy.get("rules") or [])
        for scale in (policy.get("paper_scales") or {}).values():
            add(label, scale.get("name", ""), scale.get("rules") or [])
    return keys


def _year_entry(snapshot, with_percent, in_progress):
    from .documents import attendance_text

    payload = snapshot.payload
    enrollment = snapshot.enrollment
    attendance = payload.get("attendance")
    entry = {
        "year": str(enrollment.academic_year),
        "class": payload.get("class_level") or str(enrollment.class_level),
        "section": payload.get("section_name") or enrollment.section.name,
        "group": payload.get("group") or "",
        "basis": _label(snapshot),
        "combined": isinstance(snapshot, CombinedSnapshot),
        "in_progress": in_progress,
        "system": payload.get("rulebook") or "",
        "subjects": _subjects(payload, with_percent),
        "gpa": payload.get("gpa"),
        "headline": payload.get("headline") or "",
        "result": payload.get("result") or "",
        "percent": payload.get("percent") if with_percent else None,
        "attendance": attendance_text(attendance) if attendance and attendance.get("total") else "",
    }
    if entry["combined"]:
        entry["sources"] = [
            {"exam": source.get("exam", ""), "weight": source.get("weight")} for source in payload.get("sources") or []
        ]
    return entry


def _board(student):
    from .official import official_results_for

    series = {}
    for result in official_results_for(student, confirmed_only=True):
        s = result.candidate.series
        group = series.setdefault(
            s.pk,
            {"series": s.name, "body": s.get_body_display(), "results_date": str(s.results_date or ""), "results": []},
        )
        entry = result.entry
        group["results"].append(
            {
                "id": result.pk,
                "qualification": entry.qualification if entry else "",
                "code": result.syllabus_code,
                "title": result.syllabus_title or (entry.syllabus_title if entry else ""),
                "grade": result.grade,
                "points": result.points,
                "level": entry.level if entry else "",
                "tier": entry.get_tier_display() if entry and entry.tier else "",
                "overall": result.kind == OfficialResult.Kind.OVERALL,
            }
        )
    return list(series.values())


def _predicted(student):
    """The approved predicted grade in each subject, the latest one, from the student's last enrollment."""
    from students.models import Enrollment

    last = Enrollment.objects.filter(student=student).order_by("-academic_year__start_date", "-id").first()
    if last is None:
        return []
    latest = {}
    for forecast in (
        GradeForecast.objects.filter(enrollment=last, kind=GradeForecast.Kind.PREDICTED, approved_at__isnull=False)
        .select_related("subject")
        .order_by("-as_of", "-id")
    ):
        latest.setdefault(forecast.subject_id, forecast)
    return sorted(latest.values(), key=lambda f: f.subject.name)


def _leaving(student):
    """The date the student left, from a leaving or transfer certificate that still stands."""
    from students.models import Certificate

    certificate = (
        Certificate.objects.filter(student=student, kind__in=["transfer", "leaving"], revoked_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    return (certificate.payload.get("leaving_date") or "") if certificate else ""


def build(student, *, choices=None, with_percent=True, with_predicted=True):
    """
    The transcript as it would be issued now: (payload, sources). `choices` maps a year's id to
    the result that stands for it ("exam:12" or "combined:3"); a year left out uses the default.
    """
    from academics.models import AcademicYear

    school = student.school
    choices = choices or {}
    current = AcademicYear.current_for(school)
    years, snapshots, used = [], [], []
    for entry in candidate_years(student):
        wanted = choices.get(entry["year"].pk, entry["default"])
        found = next((c for c in entry["choices"] if c[0] == wanted), None)
        if found is None:
            continue
        snapshot = found[2]
        in_progress = current is not None and entry["year"].pk == current.pk and not found[0].startswith("combined:")
        years.append(_year_entry(snapshot, with_percent, in_progress))
        kind = "combined" if isinstance(snapshot, CombinedSnapshot) else "exam"
        snapshots.append([kind, snapshot.pk, snapshot.version])
        used.append(snapshot.payload)
    board = _board(student)
    forecasts = _predicted(student) if with_predicted else []
    payload = {
        "school": {
            "name": school.name,
            "address": school.address,
            "eiin": school.eiin,
            "head": school.principal_name,
        },
        "student": {
            "name": student.full_name,
            "name_bn": student.name_bn,
            "student_id": student.student_id,
            "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else "",
            "gender": student.get_gender_display(),
            "admission_date": student.admission_date.isoformat() if student.admission_date else "",
            "left_on": _leaving(student),
            "status": student.get_status_display(),
        },
        "years": years,
        "board": board,
        "predicted": [{"subject": f.subject.name, "grade": f.grade, "as_of": f.as_of.isoformat()} for f in forecasts],
        "keys": _keys(used),
        "notes": NOTES,
        "issued_on": timezone.localdate().isoformat(),
    }
    sources = {
        "snapshots": snapshots,
        "official": [r["id"] for group in board for r in group["results"]],
        "forecasts": [f.pk for f in forecasts],
    }
    return payload, sources


# ------------------------------------------------------------------ is it still right?


def _figures(payload):
    """What a result says about the student themselves: each subject's grade, and the overall result."""
    return (
        tuple(
            sorted(
                (s.get("name", ""), s.get("letter", ""), str(s.get("grade_point"))) for s in _subjects(payload, False)
            )
        ),
        str(payload.get("gpa")),
        payload.get("result") or "",
        payload.get("headline") or "",
    )


def staleness(transcript):
    """
    {"corrected": [...], "newer": [...]}. Corrected: a result on it now says something different
    about this student, or an official result on it was amended; anyone checking it is told.
    Newer: results published since, which a reissue would add; only the school is told.
    """
    from examinations.public import published_results

    corrected, newer = [], []
    used = set()
    for kind, pk, version in transcript.sources.get("snapshots", []):
        model = CombinedSnapshot if kind == "combined" else ResultSnapshot
        old = (
            model.objects.filter(pk=pk)
            .select_related("combined" if kind == "combined" else "exam", "enrollment")
            .first()
        )
        if old is None:
            corrected.append("A result on this transcript no longer exists.")
            continue
        parent = old.combined if kind == "combined" else old.exam
        used.add((kind, parent.pk))
        if parent.status != "published":
            corrected.append(f"{parent.name} has been withdrawn.")
            continue
        if parent.publication_version != version:
            current = model.objects.filter(
                **{("combined" if kind == "combined" else "exam"): parent},
                enrollment=old.enrollment,
                version=parent.publication_version,
            ).first()
            if current is None or _figures(current.payload) != _figures(old.payload):
                corrected.append(f"{parent.name} has since been corrected.")
    for pk in transcript.sources.get("official", []):
        result = OfficialResult.objects.filter(pk=pk).first()
        if result is None or not result.is_current:
            corrected.append(f"An official result ({result.syllabus_code if result else pk}) has since been amended.")
    exams, combined = published_results(transcript.student)
    issued = transcript.created_at
    for snapshot in [*exams, *combined]:
        kind = "combined" if isinstance(snapshot, CombinedSnapshot) else "exam"
        parent = snapshot.combined if kind == "combined" else snapshot.exam
        published = getattr(parent, "published_at", None) or snapshot.created_at
        if (kind, parent.pk) not in used and published and published > issued:
            newer.append(f"{parent.name} was published after this transcript.")
    return {"corrected": corrected, "newer": newer}


# ------------------------------------------------------------------ issuing


def _outstanding(student):
    from students.certificates import _outstanding

    return _outstanding(student)


def _check_issuer(user, school, student):
    assert_actor_school(user, school)
    assert_school(school, student)
    if not user.has_perm("examinations.add_transcript"):
        raise PermissionDenied


@transaction.atomic
def issue_transcript(
    *, school, user, student, choices=None, with_percent=True, with_predicted=True, request=None, replaces=None
):
    """
    Issue a transcript. Unpaid fees do not stop it; the issuer is told and the audit log says so.
    """
    _check_issuer(user, school, student)
    payload, sources = build(student, choices=choices, with_percent=with_percent, with_predicted=with_predicted)
    if not payload["years"] and not payload["board"]:
        raise ValidationError("This student has no published or official result to put on a transcript.")
    # One issuer at a time per school, so two transcripts never take the same number.
    School.objects.select_for_update().get(pk=school.pk)
    serial = _next_serial(school, timezone.localdate().year)
    transcript = Transcript.objects.create(
        school=school,
        student=student,
        serial=serial,
        payload=payload,
        sources=sources,
        options={
            "percent": with_percent,
            "predicted": with_predicted,
            "choices": {str(k): v for k, v in (choices or {}).items()},
        },
        # The serial is part of what is fingerprinted, so two transcripts never share one.
        fingerprint=_fingerprint({**payload, "serial": serial}),
        verification_code=uuid.uuid4(),
        issued_by=user,
        replaces=replaces,
    )
    owed = _outstanding(student)
    description = f"{transcript.serial} for {student}" + (f", replacing {replaces.serial}" if replaces else "")
    if owed > 0:
        description += f", with {owed} in fees unpaid"
    AuditLog.objects.create(
        school=school,
        user=user,
        action="transcript.issued",
        model=Transcript._meta.label,
        object_id=str(transcript.pk),
        description=description,
    )
    pending = (
        request or TranscriptRequest.objects.filter(student=student, status=TranscriptRequest.Status.REQUESTED).first()
    )
    if pending is not None:
        pending.status = TranscriptRequest.Status.ISSUED
        pending.transcript = transcript
        pending.handled_by, pending.handled_at = user, timezone.now()
        pending.save(update_fields=["status", "transcript", "handled_by", "handled_at", "updated_at"])
    return transcript


@transaction.atomic
def revoke_transcript(*, school, user, transcript, reason):
    assert_actor_school(user, school)
    assert_school(school, transcript)
    if not user.has_perm("examinations.change_transcript"):
        raise PermissionDenied
    transcript = Transcript.objects.select_for_update().get(pk=transcript.pk)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Say why the transcript is being revoked.")
    if transcript.is_revoked:
        raise ValidationError(f"{transcript.serial} is already revoked.")
    transcript.revoked_at, transcript.revoked_by, transcript.revoke_reason = timezone.now(), user, reason[:200]
    transcript.save(update_fields=["revoked_at", "revoked_by", "revoke_reason", "updated_at"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="transcript.revoked",
        model=Transcript._meta.label,
        object_id=str(transcript.pk),
        description=f"{transcript.serial}: {reason}",
    )
    return transcript


@transaction.atomic
def reissue_transcript(*, school, user, transcript):
    """
    Replace a transcript with a fresh one from the results as they stand now, with the same
    choices. The old one is revoked and points to the new one.
    """
    old = Transcript.objects.select_for_update().get(pk=transcript.pk)
    if hasattr(old, "replaced_by"):
        raise ValidationError(f"{old.serial} has already been replaced by {old.replaced_by.serial}.")
    if not user.has_perm("examinations.change_transcript"):
        raise PermissionDenied
    options = old.options or {}
    choices = {int(k): v for k, v in (options.get("choices") or {}).items()}
    new = issue_transcript(
        school=school,
        user=user,
        student=old.student,
        choices=choices,
        with_percent=options.get("percent", True),
        with_predicted=options.get("predicted", True),
        replaces=old,
    )
    if not old.is_revoked:
        revoke_transcript(school=school, user=user, transcript=old, reason=f"Replaced by {new.serial}")
    return new


# ------------------------------------------------------------------ families asking


def request_transcript(*, user, student, purpose, note=""):
    """A student or guardian asks the school for a transcript. One request waits at a time."""
    purpose, note = (purpose or "").strip(), (note or "").strip()
    if not purpose:
        raise ValidationError(gettext("Say what the transcript is for, such as a university application."))
    if len(purpose) > 200 or len(note) > 300:
        raise ValidationError(gettext("Keep the purpose to 200 and the note to 300 characters."))
    if TranscriptRequest.objects.filter(student=student, status=TranscriptRequest.Status.REQUESTED).exists():
        raise ValidationError(
            gettext("A request is already with the school. You will see the transcript here once it is issued.")
        )
    item = TranscriptRequest.objects.create(
        school=student.school, student=student, requested_by=user, purpose=purpose, note=note
    )
    AuditLog.objects.create(
        school=student.school,
        user=user,
        action="transcript.requested",
        model=TranscriptRequest._meta.label,
        object_id=str(item.pk),
        description=f"For {student}: {purpose}",
    )
    return item


@transaction.atomic
def decline_request(*, school, user, request_item, reason):
    assert_actor_school(user, school)
    assert_school(school, request_item)
    if not user.has_perm("examinations.change_transcriptrequest"):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Say why, so the family knows what to do.")
    item = TranscriptRequest.objects.select_for_update().get(pk=request_item.pk)
    if item.status != TranscriptRequest.Status.REQUESTED:
        raise ValidationError("This request has already been dealt with.")
    item.status, item.decline_reason = TranscriptRequest.Status.DECLINED, reason[:200]
    item.handled_by, item.handled_at = user, timezone.now()
    item.save(update_fields=["status", "decline_reason", "handled_by", "handled_at", "updated_at"])
    return item


# ------------------------------------------------------------------ checking one


def masked(name):
    from students.certificates import mask_name

    return mask_name(name)
