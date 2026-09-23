"""
What must be right before an exam's results are published, and what is worth a second look.

Blockers stop publication: a result built on them would be wrong. Warnings do not: they are
things a head of exams should see before pressing the button.
"""

from decimal import Decimal

from .grading import scale_rules
from .models import Mark, ResultComment, UnlockRequest


def _item(level, text):
    return {"level": level, "text": text}


def publication_checklist(exam):
    """[{level: "block" | "warn", text}] for one exam, blockers first."""
    from academics.models import ClassLevel
    from students.models import Enrollment

    from .services import live_class_sheet
    from .subjects import check_choices, subject_plan

    items = []
    schedules = list(
        exam.schedules.select_related("subject", "class_level", "grade_scale").prefetch_related("components")
    )
    if not schedules:
        return [_item("block", "No papers are scheduled.")]
    rules = exam.grading_snapshot or (scale_rules(exam.grade_scale) if exam.grade_scale_id else [])
    if not rules:
        items.append(_item("block", "The exam's grade scale has no grades."))

    for schedule in schedules:
        parts = list(schedule.components.all())
        weights = [p.weight for p in parts]
        if parts and any(w is not None for w in weights):
            if any(w is None for w in weights) or sum(weights) != 100:
                items.append(
                    _item("block", f"{schedule.subject} ({schedule.class_level}): part weights do not add up to 100%.")
                )
        elif parts and sum((p.full_marks for p in parts), Decimal(0)) != schedule.full_marks:
            items.append(
                _item("block", f"{schedule.subject} ({schedule.class_level}): parts do not add up to the full marks.")
            )
        if schedule.max_grade:
            letters = [r["letter"] for r in (scale_rules(schedule.grade_scale) if schedule.grade_scale_id else rules)]
            if schedule.max_grade not in letters and schedule.max_grade.lower() not in [x.lower() for x in letters]:
                items.append(
                    _item(
                        "block",
                        f"{schedule.subject} ({schedule.class_level}): capped at {schedule.max_grade}, which its scale does not have.",
                    )
                )

    for level in ClassLevel.objects.filter(pk__in={s.class_level_id for s in schedules}).order_by("order"):
        plan = subject_plan(exam.academic_year, level)
        if plan is not None:
            wrong = [
                e.student.full_name
                for e in Enrollment.objects.filter(academic_year=exam.academic_year, class_level=level)
                .exclude(status=Enrollment.Status.LEFT)
                .select_related("student", "fourth_subject")
                .prefetch_related("chosen_subjects")
                if check_choices(e, plan)
            ]
            if wrong:
                more = f" and {len(wrong) - 3} more" if len(wrong) > 3 else ""
                items.append(
                    _item(
                        "block",
                        f"{level}: {len(wrong)} student(s) have group or subject choices that need fixing "
                        f"({', '.join(wrong[:3])}{more}), so they would be graded on the wrong papers.",
                    )
                )
        rows = live_class_sheet(exam, level)
        incomplete = [r["student"] for r in rows if not r["complete"]]
        if incomplete:
            more = f" and {len(incomplete) - 3} more" if len(incomplete) > 3 else ""
            items.append(
                _item(
                    "block",
                    f"{level}: {len(incomplete)} student(s) are missing a mark or absence "
                    f"({', '.join(incomplete[:3])}{more}).",
                )
            )
        if level.rules == "ib_dp":
            unlevelled = sorted(
                {
                    s.subject.name
                    for s in schedules
                    if s.class_level_id == level.pk and not s.subject.ib_level and not s.subject.ib_core
                }
            )
            if unlevelled:
                items.append(_item("warn", f"{level}: not marked HL or SL: {', '.join(unlevelled)}."))

    exempt = Mark.objects.filter(schedule__exam=exam, is_exempt=True).count()
    if exempt:
        items.append(_item("warn", f"{exempt} paper(s) are marked exempt. Check each exemption was approved."))
    pending = UnlockRequest.objects.filter(schedule__exam=exam, status="pending").count()
    if pending:
        items.append(_item("warn", f"{pending} unlock request(s) are waiting for a decision."))
    comments = ResultComment.objects.filter(exam=exam)
    if comments.exists():
        # Once teachers have started writing comments, say how many papers still have none.
        written = comments.filter(subject__isnull=False).values("enrollment", "subject").distinct().count()
        expected = (
            Mark.objects.filter(schedule__exam=exam, is_exempt=False)
            .values("enrollment", "schedule__subject")
            .distinct()
            .count()
        )
        if written < expected:
            items.append(_item("warn", f"{expected - written} subject comment(s) are not written yet."))
    return sorted(items, key=lambda item: item["level"] != "block")


def blockers(exam):
    return [item["text"] for item in publication_checklist(exam) if item["level"] == "block"]
