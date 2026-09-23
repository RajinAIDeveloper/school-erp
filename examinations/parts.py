"""
Setting up a paper's parts: creative and multiple choice under the national curriculum,
weighted components for Cambridge and Edexcel, criteria A to D for the IB MYP.

Parts decide how every mark on the paper is entered and totalled, so they are fixed once the
first mark is entered or the exam is published. Changing them afterwards would leave entered
marks describing a paper that no longer exists.
"""

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, assert_school
from core.models import AuditLog

from .grading import board_pass_mark, scale_rules
from .models import ExamSchedule, Mark, PaperComponent
from .rulebooks import rulebook

MAX_PARTS = 10
CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")

# key: label, note, [(code, name, raw full marks, weight or None)], highest grade (tiers)
PRESETS = {
    "national-cq-mcq": (
        "Creative and multiple choice (70 + 30)",
        "The national curriculum's usual written paper. Each part must reach 33%.",
        [("cq", "Creative", 70, None), ("mcq", "Multiple choice", 30, None)],
        "",
    ),
    "national-cq-mcq-practical": (
        "Creative, multiple choice and practical (50 + 25 + 25)",
        "National science subjects with a practical. Each part must reach 33%.",
        [("cq", "Creative", 50, None), ("mcq", "Multiple choice", 25, None), ("practical", "Practical", 25, None)],
        "",
    ),
    "cambridge-science-extended": (
        "Cambridge IGCSE science, Extended (such as Physics 0625)",
        "Paper 2 multiple choice, 40 marks at 30%; Paper 4 theory, 80 marks at 50%; practical "
        "(Paper 5 or 6), 40 marks at 20%. Check the weights against the syllabus year you teach.",
        [
            ("p2", "Paper 2 Multiple choice", 40, 30),
            ("p4", "Paper 4 Theory", 80, 50),
            ("practical", "Paper 5/6 Practical", 40, 20),
        ],
        "",
    ),
    "cambridge-science-core": (
        "Cambridge IGCSE science, Core (such as Physics 0625)",
        "Paper 1 multiple choice, 40 marks at 30%; Paper 3 theory, 80 marks at 50%; practical "
        "(Paper 5 or 6), 40 marks at 20%. Core entries can earn at most grade C, so the paper is "
        "capped at C.",
        [
            ("p1", "Paper 1 Multiple choice", 40, 30),
            ("p3", "Paper 3 Theory", 80, 50),
            ("practical", "Paper 5/6 Practical", 40, 20),
        ],
        "C",
    ),
    "myp-criteria": (
        "IB MYP criteria A to D (8 each, out of 32)",
        "Four criteria scored 0-8 and added to a total out of 32, for the MYP conversion to 1-7.",
        [
            ("a", "Criterion A", 8, None),
            ("b", "Criterion B", 8, None),
            ("c", "Criterion C", 8, None),
            ("d", "Criterion D", 8, None),
        ],
        "",
    ),
}


def plain(value):
    """A mark for a message or a form: 50 rather than 50.00, 37.5 rather than 37.50."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def locked_reason(schedule):
    """Why the parts cannot change now, or None when they can."""
    if schedule.exam.publication_version > 0:
        return "This exam's results are published, so its papers are fixed."
    if Mark.objects.filter(schedule=schedule).exists():
        return "Marks have been entered for this paper, so its parts are fixed. Clear the marks first to change them."
    return None


def scale_letters(schedule):
    """The grades the paper is graded on: its own scale if set, else the exam's."""
    exam = schedule.exam
    if schedule.grade_scale_id:
        rules = scale_rules(schedule.grade_scale)
    else:
        rules = exam.grading_snapshot or (scale_rules(exam.grade_scale) if exam.grade_scale_id else [])
    return [rule["letter"] for rule in rules]


def _check(user, schedule):
    assert_actor_school(user, schedule.school)
    assert_school(schedule.school, schedule)
    if not user.has_perm("examinations.change_examschedule"):
        raise PermissionDenied
    reason = locked_reason(schedule)
    if reason:
        raise ValidationError(reason)


def _number(raw, label):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{label}: '{raw}' is not a number.") from exc
    if not value.is_finite():
        raise ValidationError(f"{label}: '{raw}' is not a number.")
    return value


def clean_parts(schedule, parts):
    """
    Check a paper's parts as typed and return them as values to store.

    `parts` is a list of dicts with code, name, full_marks, pass_marks and weight, as strings.
    Either every part has a weight and the weights add up to 100, or none has and the raw
    marks add up to the paper's full marks.
    """
    if len(parts) > MAX_PARTS:
        raise ValidationError(f"A paper can have at most {MAX_PARTS} parts.")
    cleaned, codes = [], set()
    for index, part in enumerate(parts, start=1):
        code = (part.get("code") or "").strip().lower()
        name = (part.get("name") or "").strip()
        label = name or f"Part {index}"
        if not CODE.match(code):
            raise ValidationError(f"{label}: the code must be up to 20 lower-case letters, digits, - or _.")
        if code in codes:
            raise ValidationError(f"Two parts share the code '{code}'.")
        codes.add(code)
        if not name or len(name) > 50:
            raise ValidationError(f"Part '{code}' needs a name of up to 50 characters.")
        full = _number(part.get("full_marks"), f"{label} full marks")
        passing = _number(part.get("pass_marks") or "0", f"{label} pass marks")
        if full <= 0 or not 0 <= passing <= full:
            raise ValidationError(f"{label}: full marks must be positive and the pass mark between 0 and full marks.")
        raw_weight = (part.get("weight") or "").strip()
        weight = _number(raw_weight, f"{label} weight") if raw_weight else None
        if weight is not None and not 0 < weight <= 100:
            raise ValidationError(f"{label}: a weight is a percentage above 0 and up to 100.")
        cleaned.append({"code": code, "name": name, "full_marks": full, "pass_marks": passing, "weight": weight})

    weights = [part["weight"] for part in cleaned]
    if any(weight is not None for weight in weights):
        if any(weight is None for weight in weights):
            raise ValidationError("Give every part a weight, or none: a paper is either weighted or added up.")
        if sum(weights) != 100:
            raise ValidationError(f"The weights add up to {sum(weights)}%, not 100%.")
    elif cleaned:
        total = sum((part["full_marks"] for part in cleaned), Decimal("0"))
        if total != schedule.full_marks:
            raise ValidationError(
                f"The parts add up to {plain(total)} marks but the paper is out of {plain(schedule.full_marks)}. "
                "Change the parts, or the paper's full marks, so they agree."
            )
    return cleaned


def _replace(schedule, cleaned):
    schedule.components.all().delete()
    PaperComponent.objects.bulk_create(
        PaperComponent(school=schedule.school, schedule=schedule, order=order, **part)
        for order, part in enumerate(cleaned)
    )


@transaction.atomic
def save_parts(*, user, schedule, parts):
    """Replace a paper's parts. An empty list makes it a single score again."""
    schedule = ExamSchedule.objects.select_for_update().select_related("exam").get(pk=schedule.pk)
    _check(user, schedule)
    cleaned = clean_parts(schedule, parts)
    _replace(schedule, cleaned)
    AuditLog.objects.create(
        school=schedule.school,
        user=user,
        action="examinations.parts_saved",
        description=f"{schedule}: {', '.join(p['code'] for p in cleaned) or 'single score'}",
    )
    return cleaned


@transaction.atomic
def apply_preset(*, user, schedule, key):
    """
    Set a paper's parts from a preset and say what changed.

    An added-up preset sets the paper's full marks to the parts' total; a weighted one keeps
    the paper's full marks. Pass marks follow the class's rulebook: 33% of each part where
    the rulebook has pass marks, none where it grades by band alone.
    """
    if key not in PRESETS:
        raise ValidationError("Choose one of the listed presets.")
    schedule = ExamSchedule.objects.select_for_update().select_related("exam").get(pk=schedule.pk)
    _check(user, schedule)
    label, _note, rows, cap = PRESETS[key]
    uses_pass_marks = rulebook(schedule.exam.rules_for(schedule.class_level)).pass_marks
    weighted = any(weight is not None for *_rest, weight in rows)
    notes = [f'Parts set from "{label}".']
    if not weighted:
        total = Decimal(sum(full for _c, _n, full, _w in rows))
        if total != schedule.full_marks:
            notes.append(f"Full marks changed from {plain(schedule.full_marks)} to {plain(total)}.")
        schedule.full_marks = total
    if uses_pass_marks:
        schedule.pass_marks = board_pass_mark(schedule.full_marks)
    else:
        schedule.pass_marks = min(schedule.pass_marks, schedule.full_marks)
    if cap:
        if cap in scale_letters(schedule):
            schedule.max_grade = cap
            notes.append(f"Highest grade capped at {cap}.")
        else:
            notes.append(f"This paper's scale has no grade {cap}, so no cap was set: set it on the paper if needed.")
    schedule.save(update_fields=["full_marks", "pass_marks", "max_grade", "updated_at"])
    parts = [
        {
            "code": code,
            "name": name,
            "full_marks": str(full),
            "pass_marks": str(board_pass_mark(full)) if uses_pass_marks else "0",
            "weight": str(weight) if weight is not None else "",
        }
        for code, name, full, weight in rows
    ]
    cleaned = clean_parts(schedule, parts)
    _replace(schedule, cleaned)
    AuditLog.objects.create(
        school=schedule.school,
        user=user,
        action="examinations.parts_preset",
        description=f"{schedule}: {key}",
    )
    return " ".join(notes)
