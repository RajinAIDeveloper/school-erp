from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from academics.models import ClassLevel, SubjectTeacher
from core.access import assert_school, is_manager
from core.models import AuditLog
from messaging.notifications import notify_results_published
from students.models import Enrollment

from .grading import grade_for, pct_of, scale_rules
from .models import Exam, Mark, ResultSnapshot, UnlockRequest


def assert_can_mark(user, schedule, enrollment=None):
    if not user.has_perm("examinations.change_mark"):
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
    if enrollment:
        qs = qs.filter(section=enrollment.section)
    if not employee or not qs.exists():
        raise PermissionDenied("You can enter marks only for assigned subjects and sections.")


def active_unlock(user, schedule):
    return UnlockRequest.objects.filter(
        schedule=schedule, requested_by=user, status="approved", expires_at__gt=timezone.now()
    ).exists()


@transaction.atomic
def save_mark(*, user, schedule, enrollment, score, absent=False, expected_version=0):
    assert_school(schedule.school, enrollment)
    exam = Exam.objects.select_for_update().get(pk=schedule.exam_id)
    assert_can_mark(user, schedule, enrollment)
    if exam.status == "published" and not active_unlock(user, schedule):
        raise ValidationError("Results are published. Request a scoped unlock before editing.")
    obj = Mark.objects.select_for_update().filter(schedule=schedule, enrollment=enrollment).first()
    version = obj.version if obj else 0
    if version != expected_version:
        raise ValidationError("Another user changed this mark. Reload before saving.")
    before = str(obj.marks_obtained) if obj else "not entered"
    if obj is None:
        obj = Mark(school=schedule.school, schedule=schedule, enrollment=enrollment)
    obj.marks_obtained = None if absent else score
    obj.is_absent = absent
    obj.entered_by = user
    obj.version = version + 1
    obj.full_clean()
    if exam.status == "published":
        from .locking import published_mark_write

        with published_mark_write():
            obj.save()
    else:
        obj.save()
    AuditLog.objects.create(
        school=schedule.school,
        user=user,
        action="mark.updated",
        model=obj._meta.label,
        object_id=str(obj.pk),
        description=f"{before} -> {obj.marks_obtained}; absent={absent}; version={obj.version}",
    )
    if exam.status == "published":
        snapshot_exam(exam, user)
    return obj


def assign_ranks(rows, key="rank"):
    ranked = sorted((r for r in rows if r["complete"]), key=lambda r: Decimal(r["total"]), reverse=True)
    last = None
    rank = 0
    for index, row in enumerate(ranked, 1):
        if Decimal(row["total"]) != last:
            rank = index
            last = Decimal(row["total"])
        row[key] = rank
    for row in rows:
        row.setdefault(key, None)


def live_class_sheet(exam, class_level):
    schedules = list(exam.schedules.filter(class_level=class_level).select_related("subject"))
    enrollments = list(
        Enrollment.objects.filter(school=exam.school, academic_year=exam.academic_year, class_level=class_level)
        .select_related("student", "section")
        .order_by("section__name", "roll_number")
    )
    marks = {(m.enrollment_id, m.schedule_id): m for m in Mark.objects.filter(schedule__in=schedules)}
    rules = exam.grading_snapshot or scale_rules(exam.grade_scale)
    rows = []
    for e in enrollments:
        cells = []
        total = Decimal(0)
        points = []
        failed = 0
        complete = bool(schedules)
        for s in schedules:
            m = marks.get((e.pk, s.pk))
            missing = m is None or (not m.is_absent and m.marks_obtained is None)
            absent = bool(m and m.is_absent)
            score = Decimal(0) if missing or absent else m.marks_obtained
            percent = pct_of(score, s.full_marks)
            band = grade_for(percent, rules)
            passed = not missing and not absent and score >= s.pass_marks
            if missing:
                complete = False
            elif not passed:
                failed += 1
            total += score
            points.append(Decimal(str(band["grade_point"])) if passed else Decimal(0))
            cells.append(
                {
                    "schedule_id": s.pk,
                    "subject": s.subject.name,
                    "full_marks": str(s.full_marks),
                    "pass_marks": str(s.pass_marks),
                    "score": str(score) if not missing and not absent else None,
                    "missing": missing,
                    "absent": absent,
                    "percent": str(percent),
                    "letter": band["letter"] if passed else "F",
                    "grade_point": str(points[-1]),
                    "passed": passed,
                }
            )
        full = sum((s.full_marks for s in schedules), Decimal(0))
        gpa = (
            (sum(points) / len(points)).quantize(Decimal("0.01")) if points and not failed and complete else Decimal(0)
        )
        rows.append(
            {
                "enrollment_id": e.pk,
                "student_id": e.student_id,
                "student_code": e.student.student_id,
                "student": e.student.full_name,
                "section_id": e.section_id,
                "section": str(e.section),
                "class_level_id": class_level.pk,
                "roll": e.roll_number,
                "cells": cells,
                "total": str(total),
                "full_total": str(full),
                "percent": str(pct_of(total, full)) if complete else None,
                "gpa": str(gpa) if complete else None,
                "result": "INCOMPLETE" if not complete else ("FAIL" if failed else "PASS"),
                "complete": complete,
            }
        )
    for section_id in {r["section_id"] for r in rows}:
        assign_ranks([r for r in rows if r["section_id"] == section_id])
    assign_ranks(rows, "grade_rank")
    return rows


def analyse(rows):
    complete = [r for r in rows if r["complete"]]
    summary = {
        "appeared": len(complete),
        "incomplete": len(rows) - len(complete),
        "passed": sum(r["result"] == "PASS" for r in complete),
        "failed": sum(r["result"] == "FAIL" for r in complete),
        "average": round(sum(Decimal(r["percent"]) for r in complete) / len(complete), 2) if complete else None,
    }
    stats = {}
    for row in rows:
        for c in row["cells"]:
            st = stats.setdefault(
                c["schedule_id"],
                {"subject": c["subject"], "entered": 0, "absent": 0, "missing": 0, "passed": 0, "scores": []},
            )
            if c["missing"]:
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
    }


@transaction.atomic
def snapshot_exam(exam, user):
    exam = Exam.objects.select_for_update().get(pk=exam.pk)
    if not exam.grading_snapshot:
        exam.grading_snapshot = scale_rules(exam.grade_scale)
    if not exam.grading_snapshot:
        raise ValidationError("Configure grading rules before publishing.")
    class_ids = list(exam.schedules.values_list("class_level_id", flat=True).distinct())
    if not class_ids:
        raise ValidationError("Add exam schedules before publishing.")
    all_rows = []
    for level in ClassLevel.objects.filter(pk__in=class_ids):
        rows = live_class_sheet(exam, level)
        if not rows or any(not r["complete"] for r in rows):
            raise ValidationError(
                f"{level}: every enrolled student needs marks or an explicit absence for each scheduled subject."
            )
        all_rows.extend(rows)
    version = exam.publication_version + 1
    ResultSnapshot.objects.bulk_create(
        [
            ResultSnapshot(school=exam.school, exam=exam, enrollment_id=r["enrollment_id"], version=version, payload=r)
            for r in all_rows
        ]
    )
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
    # Announced after commit, deduplicated per student and version, and only when the
    # school has switched result messages on.
    published = list(ResultSnapshot.objects.filter(exam=exam, version=version).select_related("enrollment__student"))
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


class MarkEntryError(Exception):
    """Carries per-student messages so the grid can show each one in place."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} mark(s) could not be saved")


@transaction.atomic
def save_marks(*, user, schedule, section, rows):
    """
    Save a whole class in one go.

    `rows` is [(enrollment, score, absent, expected_version)]. Every row is validated
    before any is written: a teacher entering thirty marks should not discover on row
    twenty-nine that the first twenty-eight went in and the rest did not. Any error
    rolls the lot back and is reported against its own student.
    """
    assert_can_mark(user, schedule)
    errors = {}
    saved = []
    # The section is what the caller was authorised for, so every row must belong to it.
    # A hand-made POST could otherwise name any enrollment in the school and have its
    # marks written under a section the sender does have rights to.
    stray = [enrollment for enrollment, *_ in rows if enrollment.section_id != section.pk]
    if stray:
        raise ValidationError(
            f"{len(stray)} student(s) in this submission are not in {section}. Reload the page and try again."
        )
    for enrollment, score, absent, expected_version in rows:
        if score is None and not absent:
            continue  # nothing entered for this student yet
        try:
            saved.append(
                save_mark(
                    user=user,
                    schedule=schedule,
                    enrollment=enrollment,
                    score=score,
                    absent=absent,
                    expected_version=expected_version,
                )
            )
        except (ValidationError, PermissionDenied) as exc:
            errors[enrollment.pk] = " ".join(getattr(exc, "messages", [str(exc)]))
    if errors:
        raise MarkEntryError(errors)
    return saved
