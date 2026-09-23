"""
Combined results: several published exams weighted into one, such as an annual result.

Only published results are combined, so a combined result can never include a mark that
was never released. Each subject's combined percentage is the weighted average of that
subject's percentage in each exam the student sat it in; an absence counts as nought for its
exam, and an exemption leaves that exam out of the subject's average. The combined
percentages are then graded and worked out by the class's own rulebook, exactly as a single
exam is.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from academics.models import ClassLevel
from core.access import assert_actor_school, assert_school, is_manager
from core.models import AuditLog

from .grading import combine_units, grade_paper, scale_rules
from .models import CombinedPart, CombinedResult, CombinedSnapshot, ResultSnapshot
from .rulebooks import rulebook
from .services import assign_ranks, fingerprint

HUNDRED = Decimal(100)
CENT = Decimal("0.01")


# ------------------------------------------------------------------ setting up


def check_parts(academic_year, parts):
    """`parts` is [(exam, weight)]: two or more exams of the year, weights above 0 adding to 100."""
    if len(parts) < 2:
        raise ValidationError("Choose at least two exams to combine.")
    exams = [exam for exam, _w in parts]
    if len({exam.pk for exam in exams}) != len(exams):
        raise ValidationError("Each exam can be used once.")
    if any(exam.academic_year_id != academic_year.pk for exam in exams):
        raise ValidationError("Every exam must belong to the same academic year.")
    weights = [Decimal(str(w)) for _e, w in parts]
    if any(w <= 0 for w in weights) or sum(weights) != HUNDRED:
        raise ValidationError(f"The weights add up to {sum(weights)}%; they must add up to 100%.")


@transaction.atomic
def save_combined(*, user, school, academic_year, name, grade_scale, parts, combined=None):
    """Create or change a combined result while it is a draft."""
    assert_actor_school(user, school)
    assert_school(school, academic_year, grade_scale, *[exam for exam, _w in parts])
    if not user.has_perm("examinations.add_combinedresult"):
        raise PermissionDenied
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the combined result a name, such as Annual result.")
    check_parts(academic_year, parts)
    if combined is None:
        if CombinedResult.objects.filter(academic_year=academic_year, name=name).exists():
            raise ValidationError(f"{academic_year} already has a combined result called {name}.")
        combined = CombinedResult.objects.create(
            school=school, academic_year=academic_year, name=name, grade_scale=grade_scale
        )
    else:
        if combined.publication_version:
            raise ValidationError("A published combined result cannot be changed; create a new one.")
        combined.name, combined.grade_scale = name, grade_scale
        combined.save(update_fields=["name", "grade_scale", "updated_at"])
        combined.parts.all().delete()
    CombinedPart.objects.bulk_create(
        CombinedPart(combined=combined, exam=exam, weight=Decimal(str(weight))) for exam, weight in parts
    )
    return combined


# ------------------------------------------------------------------ working it out


def _published_rows(exam, class_level):
    return {
        s.enrollment_id: s.payload
        for s in ResultSnapshot.objects.filter(
            exam=exam, version=exam.publication_version, payload__class_level_id=class_level.pk
        )
    }


def class_levels(combined):
    ids = set()
    for part in combined.parts.select_related("exam"):
        ids |= set(part.exam.schedules.values_list("class_level_id", flat=True))
    return ClassLevel.objects.filter(pk__in=ids).order_by("order")


def live_combined_sheet(combined, class_level):
    """Every student's combined result for one class, from the exams' published results."""
    parts = list(combined.parts.select_related("exam"))
    unpublished = [p.exam.name for p in parts if p.exam.status != "published"]
    if unpublished:
        raise ValidationError(f"Publish these exams first: {', '.join(unpublished)}.")
    systems = {p.exam.rules_for(class_level) for p in parts}
    if len(systems) > 1:
        raise ValidationError(f"{class_level}: the exams follow different rulebooks, so they cannot be combined.")
    system = systems.pop()
    book = rulebook(system)
    rules = combined.grading_snapshot or scale_rules(combined.grade_scale)
    sources = [(p, _published_rows(p.exam, class_level)) for p in parts]
    students = {}
    for _part, rows in sources:
        for enrollment_id, row in rows.items():
            students[enrollment_id] = row  # the latest exam's identity details win
    sheet = []
    for enrollment_id, identity in students.items():
        subjects, trace = {}, []
        for part, rows in sources:
            row = rows.get(enrollment_id)
            if row is None:
                continue
            for cell in row["cells"]:
                entry = subjects.setdefault(cell["subject_id"], {"cell": cell, "shares": [], "scale": None})
                entry["shares"].append((part, cell))
                paper_scale = ((row.get("policy") or {}).get("paper_scales") or {}).get(str(cell["schedule_id"]))
                if paper_scale and entry["scale"] is None:
                    entry["scale"] = paper_scale["rules"]
        cells = []
        for entry in subjects.values():
            counted = [(p, c) for p, c in entry["shares"] if not c.get("exempt")]
            base = entry["cell"]
            if not counted:
                continue
            total_weight = sum((p.weight for p, _c in counted), Decimal(0))
            percent = sum(
                (p.weight * (Decimal(0) if c["absent"] else Decimal(c["percent"])) for p, c in counted), Decimal(0)
            )
            percent = (percent / total_weight).quantize(CENT)
            pass_pct = sum(
                (p.weight * Decimal(c["pass_marks"]) * HUNDRED / Decimal(c["full_marks"]) for p, c in counted),
                Decimal(0),
            )
            pass_pct = (pass_pct / total_weight).quantize(CENT)
            all_absent = all(c["absent"] for _p, c in counted)
            spec = {
                "schedule_id": base["subject_id"],
                "subject": base["subject"],
                "subject_id": base["subject_id"],
                "subject_code": base.get("subject_code", ""),
                "unit_id": base["unit_id"],
                "unit_name": base["unit_name"],
                "full_marks": HUNDRED,
                "pass_marks": pass_pct,
                "components": [],
                "role": "fourth" if base.get("is_fourth") else "main",
                "level": base.get("level", ""),
                "core": base.get("core", ""),
            }
            mark = {"absent": all_absent, "score": None if all_absent else percent, "parts": {}}
            cells.append(grade_paper(spec, mark, entry["scale"] or rules, pass_marks=book.pass_marks))
            shares = " + ".join(
                f"{p.exam.name} {'ABS' if c['absent'] else c['percent'] + '%'} × {p.weight}%" for p, c in counted
            )
            trace.append(f"{base['subject']}: {shares} = {percent}%.")
        units = combine_units(cells, rules, combine=book.combine_papers)
        outcome = book.outcome(units, rules)
        total = sum((Decimal(c["score"]) for c in cells if c["score"] is not None), Decimal(0))
        full = HUNDRED * len(cells)
        sheet.append(
            {
                **{
                    key: identity.get(key, "")
                    for key in (
                        "enrollment_id",
                        "student_id",
                        "student_code",
                        "student",
                        "student_bn",
                        "father_name",
                        "mother_name",
                        "date_of_birth",
                        "section_id",
                        "section",
                        "section_name",
                        "shift",
                        "version",
                        "class_level_id",
                        "class_level",
                        "roll",
                        "group",
                        "fourth_subject",
                    )
                },
                "combined": True,
                "sources": [
                    {
                        "exam_id": p.exam.pk,
                        "exam": p.exam.name,
                        "weight": str(p.weight),
                        "version": p.exam.publication_version,
                    }
                    for p in parts
                ],
                "system": system,
                "rulebook": book.label,
                "has_gpa": book.has_gpa,
                "has_result": book.has_result,
                "show_rank": book.ranks_by_default,
                "cells": cells,
                "subjects": units,
                "total": str(total),
                "full_total": str(full),
                "percent": str((total / full * 100).quantize(CENT)) if outcome["complete"] and full else None,
                "gpa": str(outcome["gpa"]) if outcome["gpa"] is not None else None,
                "gpa_without_fourth": (
                    str(outcome["gpa_without_fourth"]) if outcome["gpa_without_fourth"] is not None else None
                ),
                "gpa_letter": outcome["gpa_letter"],
                "points": outcome["points"],
                "headline": outcome["headline"],
                "trace": trace + outcome["trace"],
                "result": outcome["result"],
                "complete": outcome["complete"],
                "attendance": identity.get("attendance"),
                "comments": {},
                "overall_comment": "",
                "forecasts": [],
                "policy": {
                    "rulebook": system,
                    "rulebook_version": book.version,
                    "scale": combined.grade_scale.name,
                    "rules": rules,
                    "paper_scales": {},
                },
            }
        )
    for section_id in {r["section_id"] for r in sheet}:
        assign_ranks([r for r in sheet if r["section_id"] == section_id], sort_key=book.rank_key)
    assign_ranks(sheet, "grade_rank", sort_key=book.rank_key)
    sheet.sort(key=lambda r: (r["section"], r["roll"]))
    return sheet


def combined_sheet(combined, class_level):
    """The published rows when there are any, otherwise worked out now."""
    if combined.publication_version:
        return [
            s.payload
            for s in CombinedSnapshot.objects.filter(
                combined=combined, version=combined.publication_version, payload__class_level_id=class_level.pk
            ).order_by("payload__section", "payload__roll")
        ]
    return live_combined_sheet(combined, class_level)


def out_of_date(combined):
    """Exams corrected since this combined result was published, by name."""
    if not combined.publication_version:
        return []
    used = combined.sources_snapshot or {}
    return [
        part.exam.name
        for part in combined.parts.select_related("exam")
        if used.get(str(part.exam_id)) != part.exam.publication_version
    ]


@transaction.atomic
def publish_combined(combined, user):
    """Freeze every student's combined result as a new version."""
    if not is_manager(user) or not user.has_perm("examinations.change_combinedresult"):
        raise PermissionDenied
    assert_actor_school(user, combined.school)
    combined = CombinedResult.objects.select_for_update().get(pk=combined.pk)
    if combined.publication_version and not out_of_date(combined):
        raise ValidationError("Nothing has changed since this combined result was published.")
    if not combined.grading_snapshot:
        combined.grading_snapshot = scale_rules(combined.grade_scale)
    rows = []
    for level in class_levels(combined):
        level_rows = live_combined_sheet(combined, level)
        incomplete = [r["student"] for r in level_rows if not r["complete"]]
        if incomplete:
            raise ValidationError(f"{level}: incomplete combined results for {', '.join(incomplete[:5])}.")
        rows.extend(level_rows)
    if not rows:
        raise ValidationError("There are no published results to combine.")
    for row in rows:
        row["fingerprint"] = fingerprint(row)
    version = combined.publication_version + 1
    CombinedSnapshot.objects.bulk_create(
        CombinedSnapshot(
            school=combined.school, combined=combined, enrollment_id=r["enrollment_id"], version=version, payload=r
        )
        for r in rows
    )
    combined.status = "published"
    combined.publication_version = version
    combined.published_at, combined.published_by = timezone.now(), user
    combined.sources_snapshot = {
        str(p.exam_id): p.exam.publication_version for p in combined.parts.select_related("exam")
    }
    combined.save()
    AuditLog.objects.create(
        school=combined.school,
        user=user,
        action="combined_result.published",
        object_id=str(combined.pk),
        description=f"{combined} version {version}",
    )
    return combined
