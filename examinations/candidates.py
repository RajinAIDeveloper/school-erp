"""
Candidate data for awarding bodies and education boards: what an exam officer exports, and
what to check before sending it.

Nothing is sent from here. The exports are files the exam officer checks and uploads to the
body's own system (Cambridge Direct, Pearson Edexcel Online, IBIS, the board's eSIF portal).
"""

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, is_manager
from core.models import AuditLog

from .models import AccessArrangement

NAME_LIMIT = 60


def certificate_name(candidate):
    return candidate.certificate_name or candidate.student.full_name


# ------------------------------------------------------------------ awarding body entries


def entry_rows(series):
    """One row per entry, in the order the bodies' entry files use."""
    headers = [
        "Centre",
        "Candidate number",
        "UCI",
        "Name on certificate",
        "Date of birth",
        "Gender",
        "Qualification",
        "Syllabus code",
        "Option or component",
        "Tier or level",
        "Status",
    ]
    rows = []
    for candidate in series.candidates.select_related("student").prefetch_related("entries"):
        student = candidate.student
        for entry in candidate.entries.all():
            rows.append(
                [
                    series.centre_number,
                    candidate.candidate_number,
                    candidate.uci,
                    certificate_name(candidate),
                    student.date_of_birth.strftime("%d/%m/%Y") if student.date_of_birth else "",
                    student.get_gender_display(),
                    entry.qualification,
                    entry.syllabus_code,
                    entry.option_code,
                    entry.get_tier_display() if entry.tier else entry.level,
                    entry.get_status_display(),
                ]
            )
    return headers, rows


def entry_problems(series):
    """Things to put right before entries go to the body, as sentences."""
    problems = []
    if not series.centre_number:
        problems.append("The series has no centre number.")
    four_digits = series.body in ("cambridge", "pearson")
    abroad = series.body in ("cambridge", "pearson", "ib")
    from students.privacy import has_consent

    for candidate in series.candidates.select_related("student").prefetch_related("entries"):
        who = f"{candidate.candidate_number} {candidate.student.full_name}"
        name = certificate_name(candidate)
        if len(name) > NAME_LIMIT:
            problems.append(f"{who}: the name is {len(name)} characters; certificates allow {NAME_LIMIT}.")
        if not candidate.student.date_of_birth:
            problems.append(f"{who}: no date of birth.")
        if four_digits and not re.fullmatch(r"\d{4}", candidate.candidate_number):
            problems.append(f"{who}: candidate numbers are four digits for this body.")
        if not [e for e in candidate.entries.all() if e.status == "entered"]:
            problems.append(f"{who}: registered but not entered for any syllabus.")
        if abroad and not has_consent(candidate.student, "abroad"):
            problems.append(
                f"{who}: no recorded guardian consent to share data with an awarding body outside Bangladesh."
            )
    return problems


# ------------------------------------------------------------------ access arrangements


def _manager(user, school):
    assert_actor_school(user, school)
    if not (is_manager(user) and user.has_perm("examinations.change_accessarrangement")):
        raise PermissionDenied("Access arrangements are kept by the school's managers.")


@transaction.atomic
def save_arrangement(
    *, user, candidate, kind, details="", evidence="", consent_on=None, status="draft", arrangement=None
):
    """Record or update an access arrangement. It cannot be marked applied without the guardian's consent."""
    _manager(user, candidate.school)
    if kind not in AccessArrangement.Kind.values or status not in AccessArrangement.Status.values:
        raise ValidationError("Choose the kind of arrangement and its status from the lists.")
    if status != AccessArrangement.Status.DRAFT and consent_on is None:
        raise ValidationError("Record the date the guardian consented before applying.")
    if arrangement is None:
        arrangement = AccessArrangement(school=candidate.school, candidate=candidate, recorded_by=user)
    arrangement.kind, arrangement.status = kind, status
    arrangement.details, arrangement.evidence = (details or "")[:200], (evidence or "")[:200]
    arrangement.consent_on = consent_on
    arrangement.full_clean()
    arrangement.save()
    AuditLog.objects.create(
        school=candidate.school,
        user=user,
        action="access_arrangement.saved",
        description=f"{candidate.series} {candidate.candidate_number}: {arrangement.get_kind_display()}, {status}",
    )
    return arrangement


# ------------------------------------------------------------------ national board registration


BIRTH_REGISTRATION = re.compile(r"\d{17}")
NID = re.compile(r"\d{10}|\d{13}|\d{17}")


def registration_rows(academic_year, class_level):
    """The board registration (eSIF) fields for every student of a national-curriculum class."""
    from students.models import Enrollment

    from .subjects import paper_role, subject_plan

    plan = subject_plan(academic_year, class_level)
    headers = [
        "Roll",
        "Student ID",
        "Unique ID",
        "Name (English)",
        "Name (Bangla)",
        "Father (English)",
        "Father (Bangla)",
        "Father's NID",
        "Mother (English)",
        "Mother (Bangla)",
        "Mother's NID",
        "Date of birth",
        "Birth registration no.",
        "Gender",
        "Religion",
        "Group",
        "Subject codes",
        "4th subject code",
        "Previous roll",
        "Previous registration no.",
    ]
    rows, checks = [], []
    enrollments = (
        Enrollment.objects.filter(academic_year=academic_year, class_level=class_level)
        .exclude(status=Enrollment.Status.LEFT)
        .select_related("student", "section", "fourth_subject")
        .prefetch_related("chosen_subjects")
        .order_by("section__name", "roll_number")
    )
    subjects = [row.subject for row in plan.rows] if plan else []
    for e in enrollments:
        s = e.student
        codes = sorted(
            {subject.code for subject in subjects if subject.code and paper_role(e, subject, plan) == "main"}
        )
        fourth = e.fourth_subject.code if e.fourth_subject_id else ""
        rows.append(
            [
                e.roll_number,
                s.student_id,
                s.unique_id,
                s.full_name,
                s.name_bn,
                s.father_name,
                s.father_name_bn,
                s.father_nid,
                s.mother_name,
                s.mother_name_bn,
                s.mother_nid,
                s.date_of_birth.strftime("%d/%m/%Y") if s.date_of_birth else "",
                s.birth_registration_no,
                s.get_gender_display(),
                s.get_religion_display(),
                e.get_group_display() if e.group else "",
                " ".join(codes),
                fourth,
                s.previous_roll,
                s.previous_registration_no,
            ]
        )
        checks.extend(_registration_checks(e, plan))
    return headers, rows, checks


def _registration_checks(enrollment, plan):
    from .subjects import check_choices

    s = enrollment.student
    who = f"Roll {enrollment.roll_number} {s.full_name}"
    found = []
    for label, value in (
        ("Bangla name", s.name_bn),
        ("father's name", s.father_name),
        ("father's name in Bangla", s.father_name_bn),
        ("mother's name", s.mother_name),
        ("mother's name in Bangla", s.mother_name_bn),
        ("religion", s.religion),
    ):
        if not value:
            found.append(f"{who}: no {label}.")
    if not BIRTH_REGISTRATION.fullmatch(s.birth_registration_no or ""):
        found.append(f"{who}: the birth registration number should be 17 digits.")
    for label, value in (("father's", s.father_nid), ("mother's", s.mother_nid)):
        if value and not NID.fullmatch(value):
            found.append(f"{who}: the {label} NID should be 10, 13 or 17 digits.")
    if not s.photo:
        found.append(f"{who}: no photo (the board asks for 300 × 300 pixels).")
    found.extend(f"{who}: {problem}" for problem in check_choices(enrollment, plan))
    return found
