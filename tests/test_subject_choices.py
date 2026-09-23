"""
Subjects per student, end to end: the subject plan screen and its presets, the choices screen,
and the places that must agree on who sits which paper (admit cards, dashboards, progress).
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import AcademicYear, ClassLevel, ClassSubject, Section, Subject
from academics.presets import NATIONAL_9_10, copy_plan, load_national_plan
from core.dashboards import teacher_dashboard
from core.models import AssessmentSystem
from examinations.models import ExamSchedule
from examinations.services import expected_marks, save_mark
from students.models import Enrollment, Student
from students.services import ChoiceError, save_subject_choices


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ============================================================ subject plan presets


def test_the_national_plan_loads_once_and_reuses_subjects_by_code(erp):
    level = ClassLevel.objects.create(school=erp.school, name="Class 9", order=9)
    existing = Subject.objects.create(school=erp.school, name="Maths (our name)", code="109")

    added = load_national_plan(school=erp.school, user=erp.admin, academic_year=erp.year, class_level=level)
    assert added == len(NATIONAL_9_10)
    # The school's own General Mathematics is reused, not duplicated.
    assert Subject.objects.filter(school=erp.school, code="109").count() == 1
    assert ClassSubject.objects.filter(class_level=level, subject=existing).exists()
    # Bangla 1st and 2nd papers combine into one Bangla subject.
    bangla = Subject.objects.get(school=erp.school, name="Bangla")
    assert set(bangla.papers.values_list("code", flat=True)) == {"101", "102"}
    # Science appears once per group that takes it.
    science = ClassSubject.objects.filter(class_level=level, subject__code="127")
    assert set(science.values_list("group", flat=True)) == {"business", "humanities"}

    assert load_national_plan(school=erp.school, user=erp.admin, academic_year=erp.year, class_level=level) == 0


def test_copying_a_plan_keeps_rows_already_there(erp):
    next_year = AcademicYear.objects.create(
        school=erp.school, name="2027", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31)
    )
    physics = Subject.objects.create(school=erp.school, name="Physics", code="0625")
    for subject, kind in ((erp.subject, "compulsory"), (physics, "choice")):
        ClassSubject.objects.create(
            school=erp.school, academic_year=erp.year, class_level=erp.level, subject=subject, kind=kind
        )
    ClassSubject.objects.create(
        school=erp.school, academic_year=next_year, class_level=erp.level, subject=erp.subject, kind="choice"
    )

    added = copy_plan(school=erp.school, user=erp.admin, from_year=erp.year, to_year=next_year, class_level=erp.level)
    assert added == 1
    rows = dict(
        ClassSubject.objects.filter(academic_year=next_year, class_level=erp.level).values_list("subject__code", "kind")
    )
    # The row the new year already had is left as the school set it.
    assert rows == {"MATH": "choice", "0625": "choice"}

    with pytest.raises(ValidationError):
        copy_plan(school=erp.school, user=erp.admin, from_year=erp.year, to_year=erp.year, class_level=erp.level)
    empty = ClassLevel.objects.create(school=erp.school, name="Empty", order=5)
    with pytest.raises(ValidationError):
        copy_plan(school=erp.school, user=erp.admin, from_year=next_year, to_year=erp.year, class_level=empty)


def test_plan_presets_need_permission_and_the_same_school(erp):
    with pytest.raises(PermissionDenied):
        load_national_plan(school=erp.school, user=erp.teacher, academic_year=erp.year, class_level=erp.level)
    foreign_year = AcademicYear.objects.create(
        school=erp.other, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
    )
    with pytest.raises((PermissionDenied, ValidationError)):
        load_national_plan(school=erp.school, user=erp.admin, academic_year=foreign_year, class_level=erp.level)
    assert not ClassSubject.objects.exists()


def test_the_subject_plan_screen_shows_and_loads_a_plan(erp):
    client = login(erp.admin)
    page = client.get(f"/academics/subject-plan/?year={erp.year.pk}&class_level={erp.level.pk}")
    assert page.status_code == 200
    assert b"No subject plan for this class" in page.content

    response = client.post(
        "/academics/subject-plan/", {"year": erp.year.pk, "class_level": erp.level.pk, "action": "national"}
    )
    assert response.status_code == 302
    page = client.get(f"/academics/subject-plan/?year={erp.year.pk}&class_level={erp.level.pk}")
    assert b"Higher Mathematics" in page.content

    assert login(erp.teacher).get("/academics/subject-plan/").status_code == 403
    # Another school's class is never shown or changed.
    foreign = ClassLevel.objects.create(school=erp.other, name="Theirs", order=1)
    client.post("/academics/subject-plan/", {"year": erp.year.pk, "class_level": foreign.pk, "action": "national"})
    assert not ClassSubject.objects.filter(class_level=foreign).exists()


def test_plan_rows_can_be_edited_in_settings(erp):
    from django.urls import reverse

    client = login(erp.admin)
    assert client.get(reverse("settings:class_subject_list")).status_code == 200
    assert client.get(reverse("settings:class_subject_create")).status_code == 200


# ============================================================ saving choices


def rows_for(board, overrides=None):
    """One row per student as the screen would submit it, with overrides by enrollment."""
    base = {
        board.science.pk: ("science", [], board.subjects.higher_math.pk),
        board.humanities.pk: ("humanities", [], board.subjects.agriculture.pk),
    }
    base.update(overrides or {})
    return [(enrollment, *base[enrollment.pk]) for enrollment in (board.science, board.humanities)]


def save(erp, board, rows):
    return save_subject_choices(
        school=erp.school, user=erp.admin, section=board.section, academic_year=erp.year, rows=rows
    )


def test_choices_are_saved_for_the_whole_section(erp, board):
    hm, agri = board.subjects.higher_math, board.subjects.agriculture
    rows = [
        (board.science, "science", [agri.pk], hm.pk),
        (board.humanities, "humanities", [hm.pk], agri.pk),
    ]
    assert save(erp, board, rows) == 2
    board.science.refresh_from_db()
    assert board.science.fourth_subject == hm
    assert list(board.science.chosen_subjects.all()) == [agri]


def test_one_bad_row_saves_nothing_and_names_the_student(erp, board):
    hm, physics = board.subjects.higher_math, board.subjects.physics
    rows = [
        (board.science, "science", [], None),  # valid, and would clear the 4th subject
        (board.humanities, "humanities", [physics.pk], None),  # Physics is not a choice
    ]
    with pytest.raises(ChoiceError) as caught:
        save(erp, board, rows)
    assert set(caught.value.errors) == {board.humanities.pk}
    board.science.refresh_from_db()
    assert board.science.fourth_subject == hm  # untouched


def test_the_same_subject_cannot_be_both_main_and_4th(erp, board):
    hm = board.subjects.higher_math
    with pytest.raises(ChoiceError) as caught:
        save(erp, board, rows_for(board, {board.science.pk: ("science", [hm.pk], hm.pk)}))
    assert "cannot also be a main subject" in caught.value.errors[board.science.pk]


def test_a_group_the_class_does_not_offer_is_refused(erp, board):
    with pytest.raises(ChoiceError) as caught:
        save(erp, board, rows_for(board, {board.science.pk: ("business", [], None)}))
    assert "not offered" in caught.value.errors[board.science.pk]


def test_another_schools_subject_is_refused(erp, board):
    theirs = Subject.objects.create(school=erp.other, name="Theirs", code="999")
    with pytest.raises(ChoiceError) as caught:
        save(erp, board, rows_for(board, {board.science.pk: ("science", [theirs.pk], None)}))
    assert "does not exist" in caught.value.errors[board.science.pk]
    assert not board.science.chosen_subjects.exists()


def test_a_student_from_another_section_is_refused(erp, board):
    other = Section.objects.create(school=erp.school, class_level=board.level, name="B")
    stray = Enrollment.objects.create(
        school=erp.school,
        student=Student.objects.create(
            school=erp.school,
            student_id="C9-9",
            first_name="Stray",
            gender="F",
            date_of_birth=date(2011, 1, 1),
            admission_date=date(2026, 1, 1),
        ),
        academic_year=erp.year,
        class_level=board.level,
        section=other,
        roll_number=9,
    )
    with pytest.raises(ValidationError):
        save(erp, board, [(stray, "science", [], None)])


def test_choices_need_permission(erp, board):
    with pytest.raises(PermissionDenied):
        save_subject_choices(
            school=erp.school, user=erp.teacher, section=board.section, academic_year=erp.year, rows=rows_for(board)
        )


def test_a_4th_subject_is_refused_outside_the_national_curriculum(erp):
    level = ClassLevel.objects.create(
        school=erp.school, name="Year 10", order=10, assessment_system=AssessmentSystem.CAMBRIDGE
    )
    section = Section.objects.create(school=erp.school, class_level=level, name="A")
    physics = Subject.objects.create(school=erp.school, name="Physics", code="0625")
    history = Subject.objects.create(school=erp.school, name="History", code="0470")
    for subject in (physics, history):
        ClassSubject.objects.create(
            school=erp.school, academic_year=erp.year, class_level=level, subject=subject, kind="choice"
        )
    pupil = Student.objects.create(
        school=erp.school,
        student_id="Y10-1",
        first_name="Tahmid",
        gender="M",
        date_of_birth=date(2010, 5, 1),
        admission_date=date(2026, 1, 1),
    )
    enrollment = Enrollment.objects.create(
        school=erp.school, student=pupil, academic_year=erp.year, class_level=level, section=section, roll_number=3
    )
    with pytest.raises(ChoiceError) as caught:
        save_subject_choices(
            school=erp.school,
            user=erp.admin,
            section=section,
            academic_year=erp.year,
            rows=[(enrollment, "", [physics.pk], history.pk)],
        )
    assert "national curriculum" in caught.value.errors[enrollment.pk]
    # IGCSE options are ordinary choices.
    save_subject_choices(
        school=erp.school,
        user=erp.admin,
        section=section,
        academic_year=erp.year,
        rows=[(enrollment, "", [physics.pk, history.pk], None)],
    )
    assert enrollment.chosen_subjects.count() == 2


# ============================================================ the choices screen


def test_the_choices_screen_saves_and_keeps_typed_values_on_error(erp, board):
    client = login(erp.admin)
    page = client.get(f"/students/choices/?section={board.section.pk}")
    assert page.status_code == 200
    assert b"4th subject" in page.content  # a national class
    hm, agri, physics = board.subjects.higher_math, board.subjects.agriculture, board.subjects.physics
    s, h = board.science.pk, board.humanities.pk

    bad = client.post(
        "/students/choices/",
        {
            "section": board.section.pk,
            f"{s}-group": "science",
            f"{s}-chosen": [agri.pk],
            f"{s}-fourth": hm.pk,
            f"{h}-group": "humanities",
            f"{h}-chosen": [physics.pk],
            f"{h}-fourth": agri.pk,
        },
    )
    assert bad.status_code == 200
    assert b"Nothing was saved" in bad.content
    assert b"not offered to this group" in bad.content
    # The good row's typed choice is still ticked.
    assert f'name="{s}-chosen" value="{agri.pk}"'.encode() in bad.content
    assert not board.science.chosen_subjects.exists()

    good = client.post(
        "/students/choices/",
        {
            "section": board.section.pk,
            f"{s}-group": "science",
            f"{s}-chosen": [agri.pk],
            f"{s}-fourth": hm.pk,
            f"{h}-group": "humanities",
            f"{h}-fourth": agri.pk,
        },
    )
    assert good.status_code == 302
    assert list(board.science.chosen_subjects.all()) == [agri]


def test_a_class_without_a_plan_has_nothing_to_choose(erp):
    page = login(erp.admin).get(f"/students/choices/?section={erp.section.pk}")
    assert page.status_code == 200
    assert b"no subject plan" in page.content
    assert login(erp.teacher).get("/students/choices/").status_code == 403


def test_the_choices_screen_lists_open_problems(erp, board):
    board.humanities.group = ""
    board.humanities.save(update_fields=["group"])
    page = login(erp.admin).get(f"/students/choices/?section={board.section.pk}")
    assert b"No group chosen." in page.content
    assert b"still need choices fixing" in page.content


# ============================================================ every screen agrees


def test_admit_cards_list_only_each_students_papers(erp, board, monkeypatch):
    captured = {}

    def fake_pdf(school, exam, enrollments, schedules, papers_for_student=None):
        from django.http import HttpResponse

        captured.update({e.pk: {s.subject.code for s in papers_for_student(e)} for e in enrollments})
        return HttpResponse(b"%PDF-")

    monkeypatch.setattr("examinations.documents.admit_cards_pdf", fake_pdf)
    response = login(erp.admin).get(f"/exams/admit-cards.pdf?exam={board.exam.pk}&section={board.section.pk}")
    assert response.status_code == 200
    assert captured[board.science.pk] == {"101", "102", "109", "111", "136", "126"}
    assert captured[board.humanities.pk] == {"101", "102", "109", "112", "110", "134"}


def test_expected_marks_count_only_students_who_sit_the_paper(erp, board):
    s = board.schedules
    assert expected_marks(s["136"]) == 1  # Physics: Science only
    assert expected_marks(s["111"]) == 1  # Islam: the Muslim student only
    assert expected_marks(s["109"]) == 2  # General Math: everyone
    assert expected_marks(s["109"], section=board.section) == 2
    # A class with no plan: everyone sits everything, as before.
    assert expected_marks(erp.schedule) == 1


def test_the_teacher_dashboard_does_not_count_not_sat_papers_as_pending(erp, board):
    for schedule in board.schedules.values():
        for enrollment in (board.science, board.humanities):
            from examinations.subjects import takes_paper

            if takes_paper(enrollment, schedule):
                save_mark(user=erp.admin, schedule=schedule, enrollment=enrollment, score=Decimal(70))
    pending = teacher_dashboard(erp.school, erp.teacher)["pending_marks"]
    assert all(row["section"] != board.section for row in pending)


def test_an_unplanned_extra_paper_is_still_sat_by_everyone(erp, board):
    """A paper scheduled but left out of the plan is compulsory, so it is never silently dropped."""
    extra = Subject.objects.create(school=erp.school, name="Physical Education", code="147")
    schedule = ExamSchedule.objects.create(
        school=erp.school, exam=board.exam, class_level=board.level, subject=extra, full_marks=50, pass_marks=17
    )
    assert expected_marks(schedule) == 2
