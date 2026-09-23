"""
What a report card says beyond marks: subject comments with an effort grade, the class
teacher's overall comment, and the grades the school predicts, forecasts or sets as targets.

Comments belong to one exam and are fixed when its results are published. Forecasts are dated
records that build up over the year; a card shows the latest approved one up to the exam.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from academics.models import SubjectTeacher
from core.access import assert_actor_school, assert_school, is_manager
from core.models import AuditLog

from .models import GradeForecast, ResultComment

COMMENT_LIMIT = 600
EFFORT_LIMIT = 20


def effort_label(system):
    """What the effort column is called: approaches to learning in the IB, effort elsewhere."""
    return "ATL" if system in ("ib_myp", "ib_dp") else "Effort"


def teaches(user, subject, section, academic_year):
    employee = getattr(user, "employee_profile", None)
    return (
        bool(employee)
        and SubjectTeacher.objects.filter(
            school=section.school, teacher=employee, subject=subject, section=section, academic_year=academic_year
        ).exists()
    )


def is_class_teacher(user, section):
    employee = getattr(user, "employee_profile", None)
    return bool(employee) and section.class_teacher_id == employee.pk


def _locked(exam):
    """Read the exam afresh and hold it, so a comment cannot slip in while results publish."""
    from .models import Exam

    if Exam.objects.select_for_update().get(pk=exam.pk).publication_version > 0:
        raise ValidationError("This exam's results are published, so its comments are fixed.")


def _clean(effort, comment):
    effort, comment = (effort or "").strip(), (comment or "").strip()
    if len(effort) > EFFORT_LIMIT:
        raise ValidationError(f"Keep the effort grade to {EFFORT_LIMIT} characters.")
    if len(comment) > COMMENT_LIMIT:
        raise ValidationError(f"Keep each comment to {COMMENT_LIMIT} characters.")
    return effort, comment


def _write(*, user, exam, subject, section, rows):
    """Save only what changed; a row with nothing in it removes the comment."""
    existing = {
        c.enrollment_id: c
        for c in ResultComment.objects.filter(exam=exam, subject=subject, enrollment__in=[e for e, *_ in rows])
    }
    errors, changes = {}, []
    for enrollment, effort, comment in rows:
        if enrollment.section_id != section.pk or enrollment.academic_year_id != exam.academic_year_id:
            raise ValidationError(
                "A student in this submission is not in this section this year. Reload and try again."
            )
        try:
            changes.append((enrollment, *_clean(effort, comment)))
        except ValidationError as exc:
            errors[enrollment.pk] = " ".join(exc.messages)
    if errors:
        raise CommentError(errors)
    saved = 0
    for enrollment, effort, comment in changes:
        current = existing.get(enrollment.pk)
        if current and (current.effort, current.comment) == (effort, comment):
            continue
        if not effort and not comment:
            if current:
                current.delete()
                saved += 1
            continue
        if current:
            current.effort, current.comment, current.written_by = effort, comment, user
            current.save(update_fields=["effort", "comment", "written_by", "updated_at"])
        else:
            ResultComment.objects.create(
                school=exam.school,
                exam=exam,
                enrollment=enrollment,
                subject=subject,
                effort=effort,
                comment=comment,
                written_by=user,
            )
        saved += 1
    if saved:
        AuditLog.objects.create(
            school=exam.school,
            user=user,
            action="examinations.comments_saved",
            description=f"{exam} {section} {subject or 'overall'}: {saved} change(s)",
        )
    return saved


class CommentError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} comment(s) cannot be saved")


@transaction.atomic
def save_subject_comments(*, user, schedule, section, rows):
    """`rows` is [(enrollment, effort, comment)] for one paper in one section."""
    from .services import assert_can_mark

    assert_can_mark(user, schedule, section=section, permission="examinations.change_resultcomment")
    _locked(schedule.exam)
    return _write(user=user, exam=schedule.exam, subject=schedule.subject, section=section, rows=rows)


@transaction.atomic
def save_overall_comments(*, user, exam, section, rows):
    """The class teacher's comment for each student; `rows` is [(enrollment, "", comment)]."""
    assert_actor_school(user, exam.school)
    assert_school(exam.school, section)
    if not user.has_perm("examinations.change_resultcomment"):
        raise PermissionDenied
    if not (is_manager(user) or is_class_teacher(user, section)):
        raise PermissionDenied("Only the class teacher or the school's managers write overall comments.")
    _locked(exam)
    return _write(user=user, exam=exam, subject=None, section=section, rows=[(e, "", c) for e, _x, c in rows])


# ------------------------------------------------------------------------ forecasts


def may_forecast(user, subject, section, academic_year):
    return user.has_perm("examinations.add_gradeforecast") and (
        is_manager(user) or teaches(user, subject, section, academic_year)
    )


@transaction.atomic
def record_forecasts(*, user, section, subject, academic_year, kind, as_of, rows):
    """
    Add a dated record for each student given a grade. Earlier records are kept.

    `rows` is [(enrollment, grade)]. A manager's record is approved as it is made; a teacher's
    waits for a manager.
    """
    assert_actor_school(user, section.school)
    assert_school(section.school, subject, academic_year)
    if kind not in GradeForecast.Kind.values:
        raise ValidationError("Choose predicted, forecast or target.")
    if not may_forecast(user, subject, section, academic_year):
        raise PermissionDenied("You can record grades only for subjects you teach in this section.")
    if as_of is None or as_of > timezone.localdate():
        raise ValidationError("Give the date the grade was decided; it cannot be in the future.")
    approve = is_manager(user) and user.has_perm("examinations.change_gradeforecast")
    made = []
    for enrollment, grade in rows:
        grade = (grade or "").strip()
        if not grade:
            continue
        if len(grade) > 5:
            raise ValidationError(f"{enrollment.student}: a grade is at most 5 characters.")
        if enrollment.section_id != section.pk or enrollment.academic_year_id != academic_year.pk:
            raise ValidationError(
                "A student in this submission is not in this section this year. Reload and try again."
            )
        made.append(
            GradeForecast(
                school=section.school,
                enrollment=enrollment,
                subject=subject,
                kind=kind,
                grade=grade,
                as_of=as_of,
                recorded_by=user,
                approved_by=user if approve else None,
                approved_at=timezone.now() if approve else None,
            )
        )
    GradeForecast.objects.bulk_create(made)
    if made:
        AuditLog.objects.create(
            school=section.school,
            user=user,
            action="examinations.forecasts_recorded",
            description=f"{section} {subject} {kind} as of {as_of}: {len(made)} record(s)",
        )
    return len(made)


@transaction.atomic
def approve_forecasts(*, user, section, subject, academic_year, kind):
    """Approve every waiting record of one kind for one subject in one section."""
    assert_actor_school(user, section.school)
    if not (is_manager(user) and user.has_perm("examinations.change_gradeforecast")):
        raise PermissionDenied
    pending = GradeForecast.objects.filter(
        school=section.school,
        enrollment__section=section,
        enrollment__academic_year=academic_year,
        subject=subject,
        kind=kind,
        approved_at__isnull=True,
    )
    count = pending.update(approved_by=user, approved_at=timezone.now())
    if count:
        AuditLog.objects.create(
            school=section.school,
            user=user,
            action="examinations.forecasts_approved",
            description=f"{section} {subject} {kind}: {count} record(s)",
        )
    return count


# ------------------------------------------------------------------------ on the card


def card_feedback(exam, enrollment_ids, until):
    """
    Comments and approved forecasts for a set of students, ready to freeze into their results.

    Returns {enrollment_id: {"comments": {subject_id: {...}}, "overall_comment": str,
    "forecasts": [...]}}.
    """
    feedback = {pk: {"comments": {}, "overall_comment": "", "forecasts": []} for pk in enrollment_ids}
    for c in ResultComment.objects.filter(exam=exam, enrollment_id__in=enrollment_ids):
        entry = feedback[c.enrollment_id]
        if c.subject_id is None:
            entry["overall_comment"] = c.comment
        else:
            entry["comments"][str(c.subject_id)] = {"effort": c.effort, "comment": c.comment}
    seen = set()
    records = (
        GradeForecast.objects.filter(enrollment_id__in=enrollment_ids, approved_at__isnull=False, as_of__lte=until)
        .select_related("subject")
        .order_by("-as_of", "-id")
    )
    for f in records:
        key = (f.enrollment_id, f.subject_id, f.kind)
        if key in seen:
            continue
        seen.add(key)
        feedback[f.enrollment_id]["forecasts"].append(
            {
                "subject_id": f.subject_id,
                "subject": f.subject.name,
                "kind": f.kind,
                "kind_label": f.get_kind_display(),
                "grade": f.grade,
                "as_of": f.as_of.isoformat(),
            }
        )
    for entry in feedback.values():
        entry["forecasts"].sort(key=lambda f: (f["subject"], f["kind"]))
    return feedback
