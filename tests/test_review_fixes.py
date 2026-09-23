"""
Regressions for Codex's third review: PDF cards frozen like the screen card, the shape of an
IB Diploma, results that record the rules and scales that made them, and grade distributions
that order each subject by its own scale.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from academics.models import ClassLevel, Section, Subject
from core.models import AssessmentSystem
from examinations.exports import grade_distribution
from examinations.grading import dp_outcome
from examinations.models import Exam, ExamSchedule, GradeRule, ResultSnapshot, UnlockRequest
from examinations.presets import install_preset
from examinations.services import build_result_sheet, publish_exam, review_unlock, save_mark
from students.models import Enrollment, Student
from tests.test_ib_diploma import candidate, core, subject


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ============================================================ 1. PDF cards are frozen too


def test_the_pdf_card_prints_the_published_name_and_section_after_a_rename(erp, monkeypatch):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    erp.student.first_name = "Renamed"
    erp.student.save()
    erp.section.name = "Z"
    erp.section.save()

    import examinations.documents as documents

    seen = []
    real_document, real_letterhead = documents.document, documents.letterhead

    def spy_document(school, title, flow, **kwargs):
        seen.append(kwargs.get("subtitle", ""))
        return real_document(school, title, flow, **kwargs)

    def spy_letterhead(school, title, subtitle, style):
        seen.append(subtitle)
        return real_letterhead(school, title, subtitle, style)

    monkeypatch.setattr(documents, "document", spy_document)
    monkeypatch.setattr(documents, "letterhead", spy_letterhead)
    client = login(erp.admin)
    single = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf")
    bulk = client.get(f"/exams/report-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}")
    assert single.status_code == 200 and bulk.status_code == 200
    assert seen and all("Renamed" not in s and "Ayesha" in s for s in seen)
    assert all(" · Z" not in s for s in seen)


# ============================================================ 2. the shape of a Diploma


def test_five_strong_subjects_are_not_a_diploma():
    units = [subject(f"HL{i}", 7, "HL") for i in range(1, 4)] + [subject(f"SL{i}", 7, "SL") for i in range(1, 3)]
    units += [core("ee", "A"), core("tok", "A"), core("cas")]
    outcome = dp_outcome(units, [])
    assert outcome["points"] == 38
    assert "diploma conditions not met" in outcome["headline"]
    assert any("exactly six" in line for line in outcome["trace"])


def test_seven_subjects_are_not_a_diploma():
    units = candidate(sl=(5, 5, 4, 4))
    assert "conditions not met" in dp_outcome(units, [])["headline"]


@pytest.mark.parametrize("hl,sl", [((6, 6), (5, 5, 5, 5)), ((6, 6, 6, 6, 6), (5,))])
def test_a_diploma_needs_three_or_four_subjects_at_hl(hl, sl):
    outcome = dp_outcome(candidate(hl=hl, sl=sl), [])
    assert "conditions not met" in outcome["headline"]
    assert any("three or four at HL" in line for line in outcome["trace"])


def test_a_subject_without_a_level_blocks_the_estimate():
    units = candidate()
    units[0] = subject("Physics", 7, "")
    outcome = dp_outcome(units, [])
    assert any("Not marked HL or SL: Physics" in line for line in outcome["trace"])
    assert "conditions not met" in outcome["headline"]


def test_four_hl_and_two_sl_is_a_valid_diploma_shape():
    assert "conditions met" in dp_outcome(candidate(hl=(6, 6, 5, 5), sl=(5, 4)), [])["headline"]


# ============================================================ 3. rules and scales travel with the result


@pytest.fixture
def two_scales(erp):
    """A class whose Math paper is on a 9-1 scale and whose Science paper is on the exam's scale."""
    nine_to_one, _ = install_preset(erp.school, "igcse-9-1")
    erp.schedule.grade_scale = nine_to_one
    erp.schedule.save()
    science = Subject.objects.create(school=erp.school, name="Science", code="SCI")
    second = ExamSchedule.objects.create(
        school=erp.school, exam=erp.exam, class_level=erp.level, subject=science, full_marks=100, pass_marks=33
    )
    from academics.models import SubjectTeacher

    SubjectTeacher.objects.create(
        school=erp.school, teacher=erp.employee, section=erp.section, subject=science, academic_year=erp.year
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(85))
    save_mark(user=erp.teacher, schedule=second, enrollment=erp.enrollment, score=Decimal(60))
    return nine_to_one, second


def test_a_published_result_records_its_rules_version_and_scales(erp, two_scales):
    nine_to_one, _second = two_scales
    publish_exam(erp.exam, erp.admin)
    policy = ResultSnapshot.objects.get().payload["policy"]
    assert policy["rulebook"] == "own" and policy["rulebook_version"]
    assert policy["rules"] and policy["scale"] == erp.scale.name
    assert policy["paper_scales"][str(erp.schedule.pk)]["name"] == nine_to_one.name


def test_a_later_correction_does_not_regrade_a_paper_whose_scale_was_edited(erp, two_scales):
    nine_to_one, second = two_scales
    publish_exam(erp.exam, erp.admin)
    first = ResultSnapshot.objects.get(version=1).payload
    math_letter = next(c for c in first["cells"] if c["schedule_id"] == erp.schedule.pk)["letter"]
    assert math_letter == "8"
    # Someone edits the 9-1 scale after publication...
    GradeRule.objects.filter(scale=nine_to_one, letter="9").update(min_percent=Decimal("80"))
    # ...and a Science mark is corrected and republished.
    unlock = UnlockRequest.objects.create(school=erp.school, schedule=second, requested_by=erp.teacher, reason="Typo")
    review_unlock(unlock, erp.admin, True)
    save_mark(user=erp.teacher, schedule=second, enrollment=erp.enrollment, score=Decimal(62), expected_version=1)
    latest = ResultSnapshot.objects.get(version=2).payload
    assert next(c for c in latest["cells"] if c["schedule_id"] == erp.schedule.pk)["letter"] == "8"
    assert next(c for c in latest["cells"] if c["schedule_id"] == second.pk)["score"] == "62.00"


# ============================================================ 4. mixed scales in one distribution


def test_a_9_to_1_subject_in_a_cambridge_exam_is_ordered_by_its_own_scale(erp):
    cambridge, _ = install_preset(erp.school, "cambridge-igcse")
    nine_to_one, _ = install_preset(erp.school, "igcse-9-1")
    level = ClassLevel.objects.create(
        school=erp.school, name="Year 10", order=10, assessment_system=AssessmentSystem.CAMBRIDGE
    )
    section = Section.objects.create(school=erp.school, class_level=level, name="Red")
    exam = Exam.objects.create(school=erp.school, academic_year=erp.year, name="Mock", grade_scale=cambridge)
    physics = ExamSchedule.objects.create(
        school=erp.school,
        exam=exam,
        class_level=level,
        subject=Subject.objects.create(school=erp.school, name="Physics", code="0625"),
        full_marks=100,
        pass_marks=0,
    )
    maths = ExamSchedule.objects.create(
        school=erp.school,
        exam=exam,
        class_level=level,
        subject=Subject.objects.create(school=erp.school, name="Mathematics", code="4MA1"),
        full_marks=100,
        pass_marks=0,
        grade_scale=nine_to_one,
    )
    # Listed first is a student with a low Maths grade, so encounter order would put 4 before 9.
    for roll, (name, maths_score) in enumerate((("Ayan", 45), ("Mira", 95), ("Tara", 72)), 1):
        pupil = Enrollment.objects.create(
            school=erp.school,
            student=Student.objects.create(
                school=erp.school,
                student_id=f"Y10-{roll}",
                first_name=name,
                gender="F",
                date_of_birth=date(2010, 1, 1),
                admission_date=date(2026, 1, 1),
            ),
            academic_year=erp.year,
            class_level=level,
            section=section,
            roll_number=roll,
        )
        save_mark(user=erp.admin, schedule=physics, enrollment=pupil, score=Decimal(75))
        save_mark(user=erp.admin, schedule=maths, enrollment=pupil, score=Decimal(maths_score))
    sheet = build_result_sheet(exam, level)
    letters, lines = grade_distribution(sheet, sheet["rows"])
    by_subject = {line["subject"]: line for line in lines}
    maths_line = by_subject["Mathematics"]
    assert maths_line["order"][:3] == ["9", "8", "7"]
    # 9 or above: one of three; 7 or above: two of three; 4 or above: all three.
    assert maths_line["at_or_above"]["9"] == 33.3
    assert maths_line["at_or_above"]["7"] == 66.7
    assert maths_line["at_or_above"]["4"] == 100.0
    # Physics stays on Cambridge letters, and neither subject gets the other's columns.
    assert by_subject["Physics"]["counts"]["B"] == 3 and "9" not in by_subject["Physics"]["counts"]
    assert "A*" not in maths_line["counts"]
    assert letters.index("A*") < letters.index("U") < letters.index("9")
    page = login(erp.admin).get(f"/exams/results/?exam={exam.pk}&class_level={level.pk}&report=distribution")
    assert page.status_code == 200
