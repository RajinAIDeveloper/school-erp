"""
Paper parts set up on screen: presets, validation, weighting, tier caps, and the lock once
marks exist or results are published.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import ClassLevel, Section, Subject
from core.models import AssessmentSystem
from examinations.forms import ExamScheduleForm
from examinations.grading import grade_paper, scale_rules
from examinations.models import Exam, ExamSchedule, Mark, ensure_default_grade_scale
from examinations.parts import apply_preset, clean_parts, locked_reason, save_parts
from examinations.presets import install_preset
from examinations.services import live_class_sheet, publish_exam, save_mark
from students.models import Enrollment, Student


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def year10(erp):
    """A Cambridge Year 10 with one Physics paper and one pupil, no parts yet."""
    scale, _ = install_preset(erp.school, "cambridge-igcse")
    level = ClassLevel.objects.create(
        school=erp.school, name="Year 10", order=10, assessment_system=AssessmentSystem.CAMBRIDGE
    )
    section = Section.objects.create(school=erp.school, class_level=level, name="Blue")
    exam = Exam.objects.create(school=erp.school, academic_year=erp.year, name="Mock", grade_scale=scale)
    physics = Subject.objects.create(school=erp.school, name="Physics", code="0625")
    paper = ExamSchedule.objects.create(
        school=erp.school, exam=exam, class_level=level, subject=physics, full_marks=100, pass_marks=0
    )
    pupil = Enrollment.objects.create(
        school=erp.school,
        student=Student.objects.create(
            school=erp.school,
            student_id="Y10-1",
            first_name="Zara",
            gender="F",
            date_of_birth=date(2010, 5, 1),
            admission_date=date(2026, 1, 1),
        ),
        academic_year=erp.year,
        class_level=level,
        section=section,
        roll_number=1,
    )
    return SimpleNamespace(scale=scale, level=level, section=section, exam=exam, paper=paper, pupil=pupil)


def part(code, name, full, passing="0", weight=""):
    return {"code": code, "name": name, "full_marks": str(full), "pass_marks": str(passing), "weight": str(weight)}


# ============================================================ validation


@pytest.mark.parametrize(
    "parts,message",
    [
        ([part("a", "A", 60, weight=50), part("b", "B", 40)], "every part a weight, or none"),
        ([part("a", "A", 60, weight=50), part("b", "B", 40, weight=40)], "add up to 90"),
        ([part("a", "A", 60), part("b", "B", 30)], "add up to 90 marks but the paper is out of 100"),
        ([part("a", "A", 50), part("a", "Again", 50)], "share the code"),
        ([part("Bad Code", "A", 100)], "code must be"),
        ([part("a", "A", 100, passing=120)], "pass mark between"),
        ([part("a", "", 100)], "needs a name"),
        ([part("a", "A", "lots")], "not a number"),
        ([part("a", "A", 50, weight=0), part("b", "B", 50, weight=100)], "above 0"),
    ],
)
def test_parts_that_do_not_add_up_are_refused(erp, year10, parts, message):
    with pytest.raises(ValidationError) as caught:
        clean_parts(year10.paper, parts)
    assert message in " ".join(caught.value.messages)


def test_valid_parts_are_saved_in_order_and_can_be_removed(erp, year10):
    save_parts(
        user=erp.admin, schedule=year10.paper, parts=[part("theory", "Theory", 70), part("mcq", "Multiple choice", 30)]
    )
    assert list(year10.paper.components.values_list("code", "order")) == [("theory", 0), ("mcq", 1)]
    save_parts(user=erp.admin, schedule=year10.paper, parts=[])
    assert not year10.paper.components.exists()


# ============================================================ presets


def test_the_cambridge_extended_preset_reproduces_the_published_weighting(erp, year10):
    note = apply_preset(user=erp.admin, schedule=year10.paper, key="cambridge-science-extended")
    assert "Cambridge IGCSE science, Extended" in note
    year10.paper.refresh_from_db()
    assert year10.paper.full_marks == 100 and not year10.paper.max_grade
    weights = list(year10.paper.components.values_list("code", "full_marks", "weight", "pass_marks"))
    assert weights == [
        ("p2", Decimal(40), Decimal(30), Decimal(0)),
        ("p4", Decimal(80), Decimal(50), Decimal(0)),
        ("practical", Decimal(40), Decimal(20), Decimal(0)),
    ]
    # 20/40 at 30% + 60/80 at 50% + 40/40 at 20% = 15 + 37.5 + 20.
    save_mark(
        user=erp.admin,
        schedule=year10.paper,
        enrollment=year10.pupil,
        components={"p2": "20", "p4": "60", "practical": "40"},
    )
    assert Mark.objects.get(schedule=year10.paper).marks_obtained == Decimal("72.50")


def test_the_core_preset_caps_the_paper_at_c(erp, year10):
    note = apply_preset(user=erp.admin, schedule=year10.paper, key="cambridge-science-core")
    assert "capped at C" in note
    year10.paper.refresh_from_db()
    assert year10.paper.max_grade == "C"
    save_mark(
        user=erp.admin,
        schedule=year10.paper,
        enrollment=year10.pupil,
        components={"p1": "40", "p3": "80", "practical": "38"},
    )
    row = live_class_sheet(year10.exam, year10.level)[0]
    cell = row["cells"][0]
    assert cell["percent"] == "99.00"  # 30 + 50 + 38/40 of 20
    assert cell["letter"] == "C" and cell["capped"]
    assert any("capped at C" in line for line in row["trace"])


def test_a_cap_the_scale_does_not_have_is_not_set(erp, year10):
    nine_to_one, _ = install_preset(erp.school, "igcse-9-1")
    year10.paper.grade_scale = nine_to_one
    year10.paper.save()
    note = apply_preset(user=erp.admin, schedule=year10.paper, key="cambridge-science-core")
    assert "no grade C" in note
    year10.paper.refresh_from_db()
    assert year10.paper.max_grade == ""


def test_the_national_preset_sets_33_percent_pass_marks(erp):
    level = ClassLevel.objects.create(
        school=erp.school, name="Class 9", order=9, assessment_system=AssessmentSystem.NATIONAL
    )
    paper = ExamSchedule.objects.create(
        school=erp.school, exam=erp.exam, class_level=level, subject=erp.subject, full_marks=50, pass_marks=17
    )
    note = apply_preset(user=erp.admin, schedule=paper, key="national-cq-mcq-practical")
    assert "Full marks changed from 50 to 100" in note
    paper.refresh_from_db()
    assert (paper.full_marks, paper.pass_marks) == (Decimal(100), Decimal(33))
    assert list(paper.components.values_list("code", "pass_marks")) == [
        ("cq", Decimal(17)),
        ("mcq", Decimal(8)),
        ("practical", Decimal(8)),
    ]


def test_the_myp_preset_makes_a_paper_out_of_32_with_no_pass_marks(erp):
    level = ClassLevel.objects.create(
        school=erp.school, name="MYP 4", order=8, assessment_system=AssessmentSystem.IB_MYP
    )
    paper = ExamSchedule.objects.create(
        school=erp.school, exam=erp.exam, class_level=level, subject=erp.subject, full_marks=100, pass_marks=33
    )
    apply_preset(user=erp.admin, schedule=paper, key="myp-criteria")
    paper.refresh_from_db()
    assert paper.full_marks == 32
    assert paper.pass_marks <= 32
    assert list(paper.components.values_list("code", flat=True)) == ["a", "b", "c", "d"]
    assert set(paper.components.values_list("pass_marks", flat=True)) == {Decimal(0)}


def test_an_unknown_preset_is_refused(erp, year10):
    with pytest.raises(ValidationError):
        apply_preset(user=erp.admin, schedule=year10.paper, key="made-up")


# ============================================================ caps in the engine


def test_a_cap_only_lowers_grades_above_it(erp):
    rules = scale_rules(install_preset(erp.school, "cambridge-igcse")[0])
    paper = {
        "schedule_id": 1,
        "subject": "Physics",
        "subject_id": 1,
        "unit_id": 1,
        "unit_name": "Physics",
        "full_marks": Decimal(100),
        "pass_marks": Decimal(0),
        "components": [],
        "role": "main",
        "max_grade": "C",
    }
    high = grade_paper(paper, {"absent": False, "score": 91, "parts": {}}, rules, pass_marks=False)
    low = grade_paper(paper, {"absent": False, "score": 45, "parts": {}}, rules, pass_marks=False)
    assert (high["letter"], high["grade_point"], high["capped"]) == ("C", "5.00", True)
    assert (low["letter"], low["capped"]) == ("E", False)
    uncapped = grade_paper({**paper, "max_grade": ""}, {"absent": False, "score": 91, "parts": {}}, rules, False)
    assert uncapped["letter"] == "A*"


# ============================================================ locks and access


def test_parts_are_fixed_once_a_mark_is_entered(erp, year10):
    save_parts(user=erp.admin, schedule=year10.paper, parts=[part("a", "A", 100)])
    save_mark(user=erp.admin, schedule=year10.paper, enrollment=year10.pupil, components={"a": "50"})
    assert "Marks have been entered" in locked_reason(year10.paper)
    with pytest.raises(ValidationError):
        save_parts(user=erp.admin, schedule=year10.paper, parts=[])
    with pytest.raises(ValidationError):
        apply_preset(user=erp.admin, schedule=year10.paper, key="myp-criteria")
    assert list(year10.paper.components.values_list("code", flat=True)) == ["a"]


def test_parts_are_fixed_once_results_are_published(erp, year10):
    save_mark(user=erp.admin, schedule=year10.paper, enrollment=year10.pupil, score=Decimal(70))
    publish_exam(year10.exam, erp.admin)
    year10.paper.refresh_from_db()
    assert "published" in locked_reason(year10.paper)


def test_only_exam_managers_set_parts(erp, year10):
    with pytest.raises(PermissionDenied):
        save_parts(user=erp.teacher, schedule=year10.paper, parts=[])
    assert login(erp.teacher).get(f"/exams/schedule/{year10.paper.pk}/parts/").status_code == 403
    theirs = ExamSchedule.objects.create(
        school=erp.other,
        exam=Exam.objects.create(
            school=erp.other, academic_year=erp.year, name="Theirs", grade_scale=ensure_default_grade_scale(erp.other)
        ),
        class_level=ClassLevel.objects.create(school=erp.other, name="Theirs", order=1),
        subject=Subject.objects.create(school=erp.other, name="Theirs", code="T"),
    )
    assert login(erp.admin).get(f"/exams/schedule/{theirs.pk}/parts/").status_code == 404


# ============================================================ the screen


def test_the_parts_screen_saves_and_keeps_typed_rows_on_error(erp, year10):
    client = login(erp.admin)
    url = f"/exams/schedule/{year10.paper.pk}/parts/"
    page = client.get(url)
    assert page.status_code == 200
    assert b"Marked as a single score." in page.content

    bad = client.post(
        url,
        {
            "rows": "4",
            "part-0-code": "theory",
            "part-0-name": "Theory",
            "part-0-full_marks": "60",
            "part-1-code": "mcq",
            "part-1-name": "Multiple choice",
            "part-1-full_marks": "30",
        },
    )
    assert bad.status_code == 200
    assert b"add up to 90 marks" in bad.content
    assert b'value="Multiple choice"' in bad.content
    assert not year10.paper.components.exists()

    good = client.post(
        url,
        {
            "rows": "4",
            "part-0-code": "theory",
            "part-0-name": "Theory",
            "part-0-full_marks": "70",
            "part-1-code": "mcq",
            "part-1-name": "Multiple choice",
            "part-1-full_marks": "30",
        },
    )
    assert good.status_code == 302
    assert year10.paper.components.count() == 2

    preset = client.post(url, {"action": "preset", "preset": "cambridge-science-extended"})
    assert preset.status_code == 302
    assert year10.paper.components.filter(weight__isnull=False).count() == 3


def test_the_parts_screen_shows_the_lock(erp, year10):
    save_mark(user=erp.admin, schedule=year10.paper, enrollment=year10.pupil, score=Decimal(70))
    page = login(erp.admin).get(f"/exams/schedule/{year10.paper.pk}/parts/")
    assert b"Marks have been entered" in page.content
    assert b"Save parts" not in page.content


def test_the_exam_page_links_to_parts(erp, year10):
    page = login(erp.admin).get(f"/exams/{year10.exam.pk}/")
    assert f"/exams/schedule/{year10.paper.pk}/parts/".encode() in page.content


# ============================================================ the paper form


def form_data(paper, **changes):
    data = {
        "exam": paper.exam_id,
        "class_level": paper.class_level_id,
        "subject": paper.subject_id,
        "full_marks": str(paper.full_marks),
        "pass_marks": str(paper.pass_marks),
        "max_grade": paper.max_grade,
    }
    data.update(changes)
    return data


def make_form(erp, paper, **changes):
    return ExamScheduleForm(data=form_data(paper, **changes), instance=paper, school=erp.school)


def test_full_marks_are_fixed_once_marks_exist(erp, year10):
    save_mark(user=erp.admin, schedule=year10.paper, enrollment=year10.pupil, score=Decimal(70))
    form = make_form(erp, year10.paper, full_marks="80")
    assert not form.is_valid()
    assert "full_marks" in form.errors
    assert make_form(erp, year10.paper, pass_marks="10").is_valid()


def test_full_marks_must_match_added_up_parts(erp, year10):
    save_parts(user=erp.admin, schedule=year10.paper, parts=[part("a", "A", 60), part("b", "B", 40)])
    form = make_form(erp, year10.paper, full_marks="80")
    assert not form.is_valid() and "full_marks" in form.errors


def test_a_cap_must_be_a_grade_on_the_papers_scale(erp, year10):
    assert not make_form(erp, year10.paper, max_grade="9").is_valid()
    assert make_form(erp, year10.paper, max_grade="C").is_valid()
