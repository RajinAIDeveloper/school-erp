"""
IB Diploma, as a school's estimate: six subjects 1-7, core points from TOK and the Extended
Essay by the IB's matrix, and the IB's eight conditions for the diploma.

Every condition is tested on its own, against the IB's published passing criteria (ibo.org,
checked September 2026). The result is labelled a school estimate: the diploma is the IB's
to award.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from academics.models import ClassLevel, Section, Subject
from core.models import AssessmentSystem
from examinations.grading import dp_core_points, dp_outcome
from examinations.models import Exam, ExamSchedule
from examinations.presets import install_preset
from examinations.services import build_result_sheet, publish_exam, save_mark
from students.models import Enrollment, Student


def subject(name, grade, level, absent=False):
    return {
        "name": name,
        "grade_point": str(grade),
        "letter": str(grade),
        "level": level,
        "core": "",
        "absent": absent,
        "missing": False,
        "percent": "0",
    }


def core(kind, letter=None, reached=True, absent=False):
    return {
        "name": kind.upper(),
        "grade_point": "0",
        "letter": "ABS" if absent else (letter or ("Complete" if reached else "Not complete")),
        "level": "",
        "core": kind,
        "absent": absent,
        "missing": False,
        "percent": "0",
        "reached_pass_mark": reached,
    }


def candidate(hl=(6, 6, 5), sl=(5, 5, 4), ee="A", tok="B", cas=True):
    units = [subject(f"HL{i}", g, "HL") for i, g in enumerate(hl, 1)]
    units += [subject(f"SL{i}", g, "SL") for i, g in enumerate(sl, 1)]
    units += [core("ee", ee), core("tok", tok), core("cas", reached=cas)]
    return units


def test_a_typical_candidate_meets_the_conditions():
    """HL 6+6+5 = 17, SL 5+5+4 = 14, subjects 31, EE A with TOK B gives 3: 34 points."""
    outcome = dp_outcome(candidate(), [])
    assert outcome["points"] == 34
    assert "diploma conditions met" in outcome["headline"]
    assert "school estimate" in outcome["headline"]
    assert outcome["gpa"] is None and outcome["result"] is None


@pytest.mark.parametrize(
    "ee,tok,points",
    [
        ("A", "A", 3),
        ("A", "B", 3),
        ("A", "C", 2),
        ("B", "B", 2),
        ("B", "D", 1),
        ("C", "C", 1),
        ("C", "D", 0),
        ("D", "D", 0),
    ],
)
def test_the_core_points_follow_the_ib_matrix(ee, tok, points):
    assert dp_core_points(ee, tok) == points
    assert dp_core_points(tok, ee) == points  # the matrix is symmetric


@pytest.mark.parametrize("ee,tok", [("E", "A"), ("A", "E"), ("E", "E")])
def test_an_e_in_tok_or_the_essay_is_a_failing_condition(ee, tok):
    assert dp_core_points(ee, tok) is None
    outcome = dp_outcome(candidate(ee=ee, tok=tok), [])
    assert "not met" in outcome["headline"]
    assert any("failing condition" in line for line in outcome["trace"])


def test_fewer_than_24_points_fails():
    outcome = dp_outcome(candidate(hl=(4, 4, 4), sl=(4, 3, 3), ee="D", tok="D"), [])
    assert outcome["points"] == 22
    assert any("below the minimum of 24" in line for line in outcome["trace"])


def test_a_grade_1_in_any_subject_fails_even_with_enough_points():
    outcome = dp_outcome(candidate(hl=(7, 7, 7), sl=(7, 7, 1)), [])
    assert outcome["points"] >= 24
    assert any("grade 1" in line for line in outcome["trace"])


def test_more_than_two_grade_twos_fails():
    outcome = dp_outcome(candidate(hl=(7, 7, 7), sl=(2, 2, 2)), [])
    assert any("More than two grade 2s" in line for line in outcome["trace"])


def test_more_than_three_grades_of_three_or_below_fails():
    outcome = dp_outcome(candidate(hl=(7, 7, 3), sl=(3, 3, 3)), [])
    assert any("three grades of 3 or below" in line for line in outcome["trace"])


def test_fewer_than_12_points_on_hl_fails():
    outcome = dp_outcome(candidate(hl=(4, 4, 3), sl=(7, 7, 6)), [])
    assert any("on HL subjects" in line for line in outcome["trace"])


def test_with_four_hl_subjects_the_best_three_count():
    units = candidate(hl=(5, 4, 3, 7), sl=(5, 5))
    outcome = dp_outcome(units, [])
    # Best three HL: 7 + 5 + 4 = 16, not 19 and not the first three's 12.
    assert any("HL points (best three): 16" in line for line in outcome["trace"])


def test_fewer_than_9_points_on_sl_fails():
    outcome = dp_outcome(candidate(hl=(7, 7, 7), sl=(3, 3, 2)), [])
    assert any("on SL subjects" in line for line in outcome["trace"])


def test_with_only_two_sl_subjects_5_points_are_enough():
    outcome = dp_outcome(candidate(hl=(6, 6, 6, 6), sl=(3, 2)), [])
    assert not any("on SL subjects" in line for line in outcome["trace"])
    outcome = dp_outcome(candidate(hl=(6, 6, 6, 6), sl=(2, 2)), [])
    assert any("at least 5 are needed" in line for line in outcome["trace"])


def test_cas_not_met_fails():
    outcome = dp_outcome(candidate(cas=False), [])
    assert any("CAS requirements are not met" in line for line in outcome["trace"])


def test_an_n_in_a_subject_fails():
    units = candidate()
    units[0] = subject("HL1", 0, "HL", absent=True)
    outcome = dp_outcome(units, [])
    assert any("No grade (N)" in line for line in outcome["trace"])


def test_a_missing_core_element_means_the_diploma_cannot_be_confirmed():
    units = [u for u in candidate() if u.get("core") != "tok"]
    outcome = dp_outcome(units, [])
    assert "not met" in outcome["headline"]
    assert any("TOK is not recorded" in line for line in outcome["trace"])


# ------------------------------------------------------------------ end to end


def test_a_dp_class_publishes_an_estimate_with_core_points(erp):
    scale, _ = install_preset(erp.school, "ib-1-7")
    core_scale, _ = install_preset(erp.school, "ib-core")
    level = ClassLevel.objects.create(school=erp.school, name="DP1", order=11, assessment_system=AssessmentSystem.IB_DP)
    section = Section.objects.create(school=erp.school, class_level=level, name="A")
    exam = Exam.objects.create(school=erp.school, academic_year=erp.year, name="DP mock", grade_scale=scale)
    papers = {}
    for name, level_code, core_code in (
        ("Biology HL", "HL", ""),
        ("Chemistry HL", "HL", ""),
        ("Mathematics AA HL", "HL", ""),
        ("English A SL", "SL", ""),
        ("History SL", "SL", ""),
        ("Spanish B SL", "SL", ""),
        ("Theory of Knowledge", "", "tok"),
        ("Extended Essay", "", "ee"),
        ("CAS", "", "cas"),
    ):
        subj = Subject.objects.create(school=erp.school, name=name, ib_level=level_code, ib_core=core_code)
        papers[name] = ExamSchedule.objects.create(
            school=erp.school,
            exam=exam,
            class_level=level,
            subject=subj,
            full_marks=1 if core_code == "cas" else 100,
            pass_marks=1 if core_code == "cas" else 0,
            grade_scale=core_scale if core_code in ("tok", "ee") else None,
        )
    student = Student.objects.create(
        school=erp.school,
        student_id="DP-1",
        first_name="Samira",
        gender="F",
        date_of_birth=date(2008, 3, 1),
        admission_date=date(2026, 1, 1),
    )
    enrollment = Enrollment.objects.create(
        school=erp.school, student=student, academic_year=erp.year, class_level=level, section=section, roll_number=1
    )
    # 6 (72) 6 (75) 5 (64) 5 (61) 5 (60) 4 (55); TOK B (70), EE A (85) -> 3 core points; CAS done.
    scores = {
        "Biology HL": 72,
        "Chemistry HL": 75,
        "Mathematics AA HL": 64,
        "English A SL": 61,
        "History SL": 60,
        "Spanish B SL": 55,
        "Theory of Knowledge": 70,
        "Extended Essay": 85,
        "CAS": 1,
    }
    for name, score in scores.items():
        save_mark(user=erp.admin, schedule=papers[name], enrollment=enrollment, score=Decimal(score))
    row = build_result_sheet(exam, level)["rows"][0]
    assert row["points"] == 6 + 6 + 5 + 5 + 5 + 4 + 3
    assert "diploma conditions met" in row["headline"]
    cas = next(c for c in row["cells"] if c["subject"] == "CAS")
    assert cas["letter"] == "Complete"
    publish_exam(exam, erp.admin)
    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/exams/{exam.pk}/report/{student.pk}/").content.decode()
    assert "34" in body and "school estimate" in body
    assert ">GPA<" not in body and "Rank in section" not in body
