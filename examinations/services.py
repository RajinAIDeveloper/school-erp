"""
Marks, results and publication.

A result is only worth printing if it is right and cannot change behind anyone's back.
So marks are entered per section by the teacher who teaches it, validated as a whole
before any is saved, graded by the rulebook the class follows, and frozen into a versioned
snapshot at publication. After that a correction needs an approved unlock, and the whole
correction produces one new version, never one per edited cell.
"""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from academics.models import ClassLevel, SubjectTeacher
from core.access import assert_school, is_manager
from core.models import AssessmentSystem, AuditLog
from messaging.notifications import notify_results_published
from students.models import Enrollment

from .grading import combine_units, grade_paper, scale_rules
from .models import Exam, Mark, ResultSnapshot, UnlockRequest
from .rulebooks import rulebook
from .subjects import paper_role, papers_for, subject_plan, takes_paper

# Fields of a result that describe the student's own performance. Two versions that agree
# on these did not change that student's result, even if their rank moved because someone
# else's mark was corrected.
OWN_RESULT_FIELDS = ("total", "gpa", "result")


# ------------------------------------------------------------------------ permissions


def assert_can_mark(user, schedule, enrollment=None, section=None, permission="examinations.change_mark"):
    """
    May this person read or write marks for this paper, in this section?

    A teacher is authorised per subject *and* per section. Teaching Math in section A and
    Science in section B gives no access to section B's Math marks, whether saving them or
    only looking at them.
    """
    if not user.has_perm(permission):
        raise PermissionDenied
    if not user.is_superuser and user.school_id != schedule.school_id:
        raise PermissionDenied
    if is_manager(user):
        return
    employee = getattr(user, "employee_profile", None)
    qs = SubjectTeacher.objects.filter(
        school=schedule.school,
        teacher=employee,
        subject=schedule.subject,
        academic_year=schedule.exam.academic_year,
        section__class_level=schedule.class_level,
    )
    target = section or (enrollment.section if enrollment is not None else None)
    if target is not None:
        qs = qs.filter(section=target)
    if not employee or not qs.exists():
        raise PermissionDenied("You can enter marks only for assigned subjects and sections.")


def active_unlock(user, schedule):
    return UnlockRequest.objects.filter(
        schedule=schedule, requested_by=user, status="approved", expires_at__gt=timezone.now()
    ).exists()


# ------------------------------------------------------------------------ writing marks


def _normalise_parts(parts):
    return {str(code): str(Decimal(str(value)).quantize(Decimal("0.01"))) for code, value in (parts or {}).items()}


def _unchanged(obj, score, absent, parts, has_parts, exempt=False):
    """Whether a submitted row says exactly what the stored mark already says."""
    if obj is None:
        return False
    if bool(obj.is_absent) != bool(absent) or bool(obj.is_exempt) != bool(exempt):
        return False
    if absent or exempt:
        return True
    if has_parts:
        try:
            return _normalise_parts(obj.component_marks) == _normalise_parts(parts)
        except (ArithmeticError, ValueError):
            return False
    if score is None or obj.marks_obtained is None:
        return score is None and obj.marks_obtained is None
    return Decimal(str(obj.marks_obtained)) == Decimal(str(score))


@transaction.atomic
def save_mark(
    *,
    user,
    schedule,
    enrollment,
    score=None,
    absent=False,
    expected_version=0,
    components=None,
    republish=True,
    exempt=False,
):
    """
    Save one student's mark for one paper.

    Returns the mark, or None when the submission matched what was already stored: an
    unchanged row is not a change, and must not bump a version or trigger a re-publication.
    With `republish=False` the caller takes responsibility for snapshotting once at the end,
    which is how a whole corrected grid becomes a single new version.
    """
    assert_school(schedule.school, enrollment)
    exam = Exam.objects.select_for_update().get(pk=schedule.exam_id)
    assert_can_mark(user, schedule, enrollment)
    if not takes_paper(enrollment, schedule):
        raise ValidationError(f"{enrollment.student.full_name} does not sit this paper.")
    if exam.status == "published" and not active_unlock(user, schedule):
        raise ValidationError("Results are published. Request a scoped unlock before editing.")
    has_parts = schedule.components.exists()
    obj = Mark.objects.select_for_update().filter(schedule=schedule, enrollment=enrollment).first()
    version = obj.version if obj else 0
    if version != expected_version:
        raise ValidationError("Another user changed this mark. Reload before saving.")
    if _unchanged(obj, score, absent, components, has_parts, exempt):
        return None
    if bool(exempt) != bool(obj and obj.is_exempt) and not is_manager(user):
        # Excusing a student changes their result, so it is the school's decision to make.
        raise PermissionDenied("Only the school's managers may excuse a student from a paper, or undo it.")
    before = _describe(obj) if obj else "not entered"
    if obj is None:
        obj = Mark(school=schedule.school, schedule=schedule, enrollment=enrollment)
    obj.is_absent = absent and not exempt
    obj.is_exempt = exempt
    if absent or exempt:
        obj.marks_obtained = None
        obj.component_marks = {}
    elif has_parts:
        # Stored as text: JSON has no decimal type, and a float would round the marks.
        obj.component_marks = {str(code): str(value) for code, value in (components or {}).items()}
        obj.marks_obtained = None  # the model totals the parts in clean()
    else:
        obj.marks_obtained = score
        obj.component_marks = {}
    obj.entered_by = user
    obj.version = version + 1
    obj.full_clean()
    _write(exam, obj.save)
    AuditLog.objects.create(
        school=schedule.school,
        user=user,
        action="mark.updated",
        model=obj._meta.label,
        object_id=str(obj.pk),
        description=(f"{before} -> {_describe(obj)}; parts={obj.component_marks or '-'}; version={obj.version}"),
    )
    if exam.status == "published" and republish:
        snapshot_exam(exam, user)
    return obj


def _describe(mark):
    if mark.is_exempt:
        return "exempt"
    return "absent" if mark.is_absent else str(mark.marks_obtained)


def _write(exam, operation):
    """Run a mark write, passing the database lock only for a published exam."""
    if exam.status == "published":
        from .locking import published_mark_write

        with published_mark_write():
            operation()
    else:
        operation()


@transaction.atomic
def clear_mark(*, user, schedule, enrollment, expected_version):
    """
    Take a mark back to "not entered".

    This is how an absence recorded by mistake is undone. It is refused once results are
    published: a published result must have a mark or an absence for every paper, so the
    correction there is to enter the score, not to leave a hole.
    """
    exam = Exam.objects.select_for_update().get(pk=schedule.exam_id)
    assert_can_mark(user, schedule, enrollment)
    obj = Mark.objects.select_for_update().filter(schedule=schedule, enrollment=enrollment).first()
    if obj is None:
        return False
    if obj.version != expected_version:
        raise ValidationError("Another user changed this mark. Reload before saving.")
    if exam.status == "published":
        raise ValidationError("A published mark cannot be cleared. Enter the score, or mark the student absent.")
    if obj.is_exempt and not is_manager(user):
        raise PermissionDenied("Only the school's managers may undo an exemption.")
    before = _describe(obj)
    pk = obj.pk
    obj.delete()
    AuditLog.objects.create(
        school=schedule.school,
        user=user,
        action="mark.cleared",
        model=Mark._meta.label,
        object_id=str(pk),
        description=f"{before} -> not entered",
    )
    return True


class MarkEntryError(Exception):
    """Carries per-student messages so the grid can show each one in place."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} mark(s) could not be saved")


@transaction.atomic
def save_marks(*, user, schedule, section, rows):
    """
    Save a whole class in one go.

    `rows` is [(enrollment, score, absent, expected_version)] or the same with a fifth item,
    the parts {code: score}, for a paper marked in parts. Every row is validated before any
    is written, and any error rolls the lot back and is reported against its own student.

    A row left completely blank clears an existing mark, which is how "clear all absences"
    works. Unchanged rows are left alone. On a published exam the whole submission, however
    many cells it corrects, becomes exactly one new published version.
    """
    assert_can_mark(user, schedule, section=section)
    # The section is what the caller was authorised for, so every row must belong to it.
    # A hand-made POST could otherwise name any enrollment in the school and have its
    # marks written under a section the sender does have rights to.
    stray = [row[0] for row in rows if row[0].section_id != section.pk]
    if stray:
        raise ValidationError(
            f"{len(stray)} student(s) in this submission are not in {section}. Reload the page and try again."
        )
    exam = Exam.objects.select_for_update().get(pk=schedule.exam_id)
    errors, saved = {}, []
    for row in rows:
        enrollment, score, absent, expected_version = row[:4]
        parts = row[4] if len(row) > 4 else None
        exempt = bool(row[5]) if len(row) > 5 else False
        filled_parts = {code: value for code, value in (parts or {}).items() if value not in (None, "")}
        try:
            if score is None and not absent and not exempt and not filled_parts:
                if clear_mark(user=user, schedule=schedule, enrollment=enrollment, expected_version=expected_version):
                    saved.append(enrollment.pk)
                continue
            mark = save_mark(
                user=user,
                schedule=schedule,
                enrollment=enrollment,
                score=score,
                absent=absent,
                expected_version=expected_version,
                components=filled_parts or None,
                republish=False,
                exempt=exempt,
            )
            if mark is not None:
                saved.append(mark)
        except (ValidationError, PermissionDenied) as exc:
            errors[enrollment.pk] = " ".join(getattr(exc, "messages", [str(exc)]))
    if errors:
        raise MarkEntryError(errors)
    if saved and exam.status == "published":
        snapshot_exam(exam, user)
    return saved


# ------------------------------------------------------------------------ building a result


def class_attendance(enrollment_ids, until):
    """
    Attendance in the exam's year up to the exam, as it stood when the result was built, for a
    whole class in one query: {enrollment_id: figures}, leaving out students with no register.
    """
    from attendance.models import StudentAttendance

    records = StudentAttendance.objects.filter(enrollment_id__in=enrollment_ids)
    if until is not None:
        records = records.filter(date__lte=until)
    counts = records.values("enrollment_id").annotate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    return {
        row["enrollment_id"]: {
            "total": row["total"],
            "present": row["present"],
            "percent": round(row["present"] * 100 / row["total"]),
            # The cut-off is printed, so a family can see what period the figure covers.
            "until": until.isoformat() if until else "",
        }
        for row in counts
        if row["total"]
    }


def _paper_spec(schedule, role):
    subject = schedule.subject
    unit = subject.combines_into or subject
    return {
        "schedule_id": schedule.pk,
        "subject": subject.name,
        "subject_id": subject.pk,
        "subject_code": subject.code or "",
        "unit_id": unit.pk,
        "unit_name": unit.name,
        "full_marks": schedule.full_marks,
        "pass_marks": schedule.pass_marks,
        "components": [(c.code, c.name, c.full_marks, c.pass_marks) for c in schedule.components.all()],
        "role": role,
        "level": subject.ib_level,
        "core": subject.ib_core,
        "max_grade": schedule.max_grade,
    }


def _exempt_cell(schedule, role):
    """A paper the student was excused from: shown on the card, left out of the result."""
    spec = _paper_spec(schedule, role)
    return {
        "schedule_id": spec["schedule_id"],
        "subject": spec["subject"],
        "subject_id": spec["subject_id"],
        "subject_code": spec["subject_code"],
        "unit_id": spec["unit_id"],
        "unit_name": spec["unit_name"],
        "full_marks": str(spec["full_marks"]),
        "pass_marks": str(spec["pass_marks"]),
        "score": None,
        "missing": False,
        "absent": False,
        "exempt": True,
        "percent": "0",
        "letter": "EX",
        "grade_point": "0",
        "passed": True,
        "failed_part": False,
        "capped": False,
        "components": [],
        "is_fourth": False,
        "level": spec.get("level", ""),
        "core": spec.get("core", ""),
        "reached_pass_mark": False,
    }


def _mark_dict(mark):
    if mark is None:
        return None
    return {"absent": mark.is_absent, "score": mark.marks_obtained, "parts": mark.component_marks or {}}


def live_class_sheet(exam, class_level):
    """Every student's result for one class, worked out from the marks as they stand now."""
    schedules = list(
        exam.schedules.filter(class_level=class_level)
        .select_related("subject__combines_into", "grade_scale")
        .prefetch_related("components", "grade_scale__rules")
        .order_by("subject__code", "subject__name")
    )
    enrollments = list(
        Enrollment.objects.filter(school=exam.school, academic_year=exam.academic_year, class_level=class_level)
        .select_related("student", "section__class_level", "fourth_subject")
        .prefetch_related("chosen_subjects")
        .order_by("section__name", "roll_number")
    )
    marks = {(m.enrollment_id, m.schedule_id): m for m in Mark.objects.filter(schedule__in=schedules)}
    rules = exam.grading_snapshot or scale_rules(exam.grade_scale)
    system = exam.rules_for(class_level)
    book = rulebook(system)
    # A paper on another board's scale (Edexcel 9-1 Maths in a Cambridge year) is graded on
    # its own scale; everything else on the exam's.
    frozen = exam.paper_grading_snapshot or {}
    paper_rules = {
        s.pk: (frozen.get(str(s.pk)) or scale_rules(s.grade_scale)) if s.grade_scale_id else rules for s in schedules
    }
    policy = {
        "rulebook": system,
        "rulebook_version": book.version,
        "scale": exam.grade_scale.name if exam.grade_scale_id else "",
        "rules": rules,
        "paper_scales": {
            str(s.pk): {"name": s.grade_scale.name, "rules": paper_rules[s.pk]} for s in schedules if s.grade_scale_id
        },
    }
    plan = subject_plan(exam.academic_year, class_level)
    until = exam.end_date or timezone.localdate()
    show_rank = book.show_rank(exam)
    from .feedback import card_feedback, effort_label

    feedback = card_feedback(exam, [e.pk for e in enrollments], until)
    attendance = class_attendance([e.pk for e in enrollments], until)
    rows = []
    for e in enrollments:
        taken = papers_for(e, schedules, plan)
        excused = [(s, r) for s, r in taken if (m := marks.get((e.pk, s.pk))) is not None and m.is_exempt]
        cells = [
            grade_paper(
                _paper_spec(schedule, role if book.fourth_subject else "main"),
                _mark_dict(marks.get((e.pk, schedule.pk))),
                paper_rules[schedule.pk],
                pass_marks=book.pass_marks,
            )
            for schedule, role in taken
            if (schedule, role) not in excused
        ]
        units = combine_units(cells, rules, combine=book.combine_papers)
        if not cells and excused:
            # Excused from every paper: nothing to grade, and nothing missing either.
            outcome = {
                "complete": True,
                "result": None,
                "gpa": None,
                "gpa_without_fourth": None,
                "gpa_letter": None,
                "points": None,
                "headline": "Exempt from every paper",
                "trace": ["Every paper is marked exempt, so there is no result to work out."],
            }
        else:
            outcome = book.outcome(units, rules)
        total = sum((Decimal(c["score"]) for c in cells if c["score"] is not None), Decimal(0))
        full = sum((Decimal(c["full_marks"]) for c in cells), Decimal(0))
        student = e.student
        rows.append(
            {
                "enrollment_id": e.pk,
                "student_id": e.student_id,
                "student_code": student.student_id,
                "student": student.full_name,
                "student_bn": student.name_bn,
                "father_name": student.father_name,
                "mother_name": student.mother_name,
                "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else "",
                "section_id": e.section_id,
                "section": str(e.section),
                "section_name": e.section.name,
                "shift": e.section.get_shift_display() if e.section.shift else "",
                "version": e.section.get_version_display() if e.section.version else "",
                "class_level_id": class_level.pk,
                "class_level": class_level.name,
                "roll": e.roll_number,
                "group": e.get_group_display() if e.group else "",
                "fourth_subject": e.fourth_subject.name if (book.fourth_subject and e.fourth_subject) else "",
                "system": system,
                "rulebook": book.label,
                "policy": policy,
                "has_gpa": book.has_gpa,
                "has_result": book.has_result,
                "show_rank": show_rank,
                "cells": cells + [_exempt_cell(s, r) for s, r in excused],
                "cells_graded": bool(cells),
                "subjects": units,
                "total": str(total),
                "full_total": str(full),
                "percent": str((total / full * 100).quantize(Decimal("0.01")))
                if (outcome["complete"] and full)
                else None,
                "gpa": str(outcome["gpa"]) if outcome["gpa"] is not None else None,
                "gpa_without_fourth": (
                    str(outcome["gpa_without_fourth"]) if outcome["gpa_without_fourth"] is not None else None
                ),
                "gpa_letter": outcome["gpa_letter"],
                "points": outcome["points"],
                "headline": outcome["headline"],
                "trace": [
                    f"{c['subject']}: capped at {c['letter']}, the highest grade for this tier."
                    for c in cells
                    if c.get("capped")
                ]
                + outcome["trace"],
                "result": outcome["result"],
                "complete": outcome["complete"],
                "attendance": attendance.get(e.pk),
                "comments": feedback[e.pk]["comments"],
                "overall_comment": feedback[e.pk]["overall_comment"],
                "forecasts": feedback[e.pk]["forecasts"],
                "effort_label": effort_label(system),
            }
        )
    for section_id in {r["section_id"] for r in rows}:
        assign_ranks([r for r in rows if r["section_id"] == section_id], sort_key=book.rank_key)
    assign_ranks(rows, "grade_rank", sort_key=book.rank_key)
    return rows


def assign_ranks(rows, key="rank", sort_key=None):
    """
    Rank the complete results; ties share a place.

    The order comes from the rulebook: under board rules passes first, then GPA, then total,
    so a failing student never outranks a passing one; IB by points; the school's own rules by
    total. Positions are always worked out, and shown only where the rulebook or the exam says.
    """
    sort_key = sort_key or (lambda r: (Decimal(r["total"]),))
    # A student with nothing graded (exempt from every paper) takes no position.
    ranked = sorted((r for r in rows if r["complete"] and r.get("cells_graded", True)), key=sort_key, reverse=True)
    last, rank = None, 0
    for index, row in enumerate(ranked, 1):
        current = sort_key(row)
        if current != last:
            rank, last = index, current
        row[key] = rank
    for row in rows:
        row.setdefault(key, None)


def rank_within(rows, field):
    """Merit positions within each value of `field` (group, shift, version), for merit lists."""
    sort_key = rulebook(rows[0]["system"]).rank_key if rows else None
    for value in {r.get(field, "") for r in rows}:
        subset = [dict(r) for r in rows if r.get(field, "") == value]
        assign_ranks(subset, "merit", sort_key=sort_key)
        positions = {r["enrollment_id"]: r["merit"] for r in subset}
        for row in rows:
            if row.get(field, "") == value:
                row["merit"] = positions[row["enrollment_id"]]
    return rows


def analyse(rows):
    complete = [r for r in rows if r["complete"]]
    summary = {
        "appeared": len(complete),
        "incomplete": len(rows) - len(complete),
        "passed": sum(r["result"] == "PASS" for r in complete),
        "failed": sum(r["result"] == "FAIL" for r in complete),
        "average": (
            round(sum(Decimal(r["percent"]) for r in complete if r["percent"]) / len(complete), 2) if complete else None
        ),
    }
    stats = {}
    for row in rows:
        for c in row["cells"]:
            st = stats.setdefault(
                c["schedule_id"],
                {"subject": c["subject"], "entered": 0, "absent": 0, "missing": 0, "passed": 0, "scores": []},
            )
            st.setdefault("exempt", 0)
            if c.get("exempt"):
                st["exempt"] += 1
            elif c["missing"]:
                st["missing"] += 1
            else:
                st["entered"] += 1
                if c["absent"]:
                    st["absent"] += 1
                else:
                    st["scores"].append(Decimal(c["score"]))
                st["passed"] += int(c["passed"])
    for st in stats.values():
        scores = st.pop("scores")
        st["average"] = round(sum(scores) / len(scores), 2) if scores else None
        st["highest"] = max(scores) if scores else None
        st["lowest"] = min(scores) if scores else None
        st["failed"] = st["entered"] - st["passed"]
        st["pass_pct"] = round(st["passed"] * 100 / st["entered"], 1) if st["entered"] else None
    return summary, list(stats.values())


def sheet_columns(rows):
    """Every paper that appears on any row, in order, for a table with one column per paper."""
    seen, columns = set(), []
    for row in rows:
        for cell in row["cells"]:
            if cell["schedule_id"] not in seen:
                seen.add(cell["schedule_id"])
                columns.append((cell["schedule_id"], cell["subject"], cell.get("subject_code", "")))
    return columns


def subject_columns(rows):
    """Every graded subject (combined papers count once) that appears on any row, in order."""
    seen, columns = set(), []
    for row in rows:
        for unit in row.get("subjects") or []:
            if unit["name"] not in seen:
                seen.add(unit["name"])
                columns.append(unit["name"])
    return columns


def build_result_sheet(exam, class_level, section=None):
    if exam.status == "published":
        rows = [
            dict(s.payload)
            for s in ResultSnapshot.objects.filter(
                exam=exam, version=exam.publication_version, payload__class_level_id=class_level.pk
            )
        ]
    else:
        rows = live_class_sheet(exam, class_level)
    if section:
        rows = [r for r in rows if r["section_id"] == section.pk]
    rows.sort(key=lambda r: (r["grade_rank"] is None, r["grade_rank"] or 0, r["section"], r["roll"]))
    summary, stats = analyse(rows)
    return {
        "exam": exam,
        "class_level": class_level,
        "section": section,
        "rows": rows,
        "summary": summary,
        "subject_stats": stats,
        "columns": sheet_columns(rows),
        "subject_names": subject_columns(rows),
        "board": exam.rules_for(class_level) == AssessmentSystem.NATIONAL,
        "rulebook": rulebook(exam.rules_for(class_level)),
    }


# ------------------------------------------------------------------------ publishing


def fingerprint(payload):
    """
    A short digest of everything a card shows.

    Printed on the card and shown on the verification page, so a card whose marks or name
    were altered after printing no longer matches what the school published.
    """
    canonical = json.dumps(
        {k: v for k, v in payload.items() if k != "fingerprint"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12].upper()
    return "-".join(digest[i : i + 4] for i in range(0, 12, 4))


def _own_result(payload):
    return (
        tuple(payload.get(field) for field in OWN_RESULT_FIELDS),
        tuple((c["schedule_id"], c["score"], c["absent"]) for c in payload.get("cells", [])),
    )


@transaction.atomic
def snapshot_exam(exam, user):
    """
    Freeze every student's result as a new published version.

    Everything a card prints — name, class, roll, attendance up to the exam, every mark and
    part — goes into the snapshot, so two prints of the same version are identical whenever
    they are made. On a re-publication only families whose own result changed are told.
    """
    exam = Exam.objects.select_for_update().get(pk=exam.pk)
    if not exam.grading_snapshot:
        exam.grading_snapshot = scale_rules(exam.grade_scale)
    if not exam.paper_grading_snapshot:
        # Papers on their own scale are frozen with the exam's, so a correction published
        # later cannot regrade other students because someone edited that scale meanwhile.
        exam.paper_grading_snapshot = {
            str(s.pk): scale_rules(s.grade_scale)
            for s in exam.schedules.filter(grade_scale__isnull=False).select_related("grade_scale")
        }
    if not exam.grading_snapshot:
        raise ValidationError("Configure grading rules before publishing.")
    class_ids = list(exam.schedules.values_list("class_level_id", flat=True).distinct())
    if not class_ids:
        raise ValidationError("Add exam schedules before publishing.")
    all_rows = []
    for level in ClassLevel.objects.filter(pk__in=class_ids):
        rows = live_class_sheet(exam, level)
        incomplete = [r for r in rows if not r["complete"]]
        if not rows or incomplete:
            names = ", ".join(r["student"] for r in incomplete[:5])
            more = f" and {len(incomplete) - 5} more" if len(incomplete) > 5 else ""
            raise ValidationError(
                f"{level}: every student needs a mark or an explicit absence for each paper they sit"
                + (f" (missing: {names}{more})." if incomplete else ".")
            )
        all_rows.extend(rows)
    for row in all_rows:
        row["fingerprint"] = fingerprint(row)
    previous = {
        s.enrollment_id: s.payload for s in ResultSnapshot.objects.filter(exam=exam, version=exam.publication_version)
    }
    version = exam.publication_version + 1
    ResultSnapshot.objects.bulk_create(
        [
            ResultSnapshot(school=exam.school, exam=exam, enrollment_id=r["enrollment_id"], version=version, payload=r)
            for r in all_rows
        ]
    )
    from .facts import record_exam

    # The analytics tables follow the published version, in the same transaction.
    record_exam(exam, all_rows, version)
    first = exam.publication_version == 0
    exam.status = "published"
    exam.publication_version = version
    exam.published_by = user
    exam.published_at = timezone.now()
    exam.save()
    AuditLog.objects.create(
        school=exam.school,
        user=user,
        action="results.published",
        object_id=str(exam.pk),
        description=f"Snapshot version {version}",
    )
    changed = {
        r["enrollment_id"]
        for r in all_rows
        if first or r["enrollment_id"] not in previous or _own_result(previous[r["enrollment_id"]]) != _own_result(r)
    }
    published = list(
        ResultSnapshot.objects.filter(exam=exam, version=version, enrollment_id__in=changed).select_related(
            "enrollment__student"
        )
    )
    # Announced after commit, deduplicated per student and version, and only when the school
    # has switched result messages on. A correction reaches only the families it affects.
    transaction.on_commit(lambda: notify_results_published(exam, published))
    return exam


@transaction.atomic
def publish_exam(exam, user):
    if not is_manager(user) or not user.has_perm("examinations.change_exam"):
        raise PermissionDenied
    assert_school(exam.school, user) if not user.is_superuser else None
    locked = Exam.objects.select_for_update().get(pk=exam.pk)
    if locked.status == "published":
        return locked
    from .checklist import blockers

    problems = blockers(locked)
    if problems:
        raise ValidationError(problems)
    return snapshot_exam(locked, user)


@transaction.atomic
def review_unlock(request_obj, user, approve):
    if not is_manager(user) or not user.has_perm("examinations.change_exam"):
        raise PermissionDenied
    if not user.is_superuser and request_obj.school_id != user.school_id:
        raise PermissionDenied
    obj = UnlockRequest.objects.select_for_update().get(pk=request_obj.pk)
    if obj.status != "pending":
        raise ValidationError("This request has already been reviewed.")
    obj.status = "approved" if approve else "rejected"
    obj.reviewed_by = user
    obj.expires_at = timezone.now() + timedelta(hours=48) if approve else None
    obj.save()
    AuditLog.objects.create(
        school=obj.school, user=user, action="marks.unlock." + obj.status, object_id=str(obj.pk), description=obj.reason
    )
    return obj


def expected_marks(schedule, section=None):
    """How many students sit this paper, for progress counts and dashboards."""
    enrollments = Enrollment.objects.filter(
        school=schedule.school,
        academic_year=schedule.exam.academic_year,
        class_level=schedule.class_level,
        status=Enrollment.Status.ENROLLED,
    ).select_related("student")
    if section is not None:
        enrollments = enrollments.filter(section=section)
    plan = subject_plan(schedule.exam.academic_year, schedule.class_level)
    return sum(1 for e in enrollments.prefetch_related("chosen_subjects") if paper_role(e, schedule.subject, plan))
