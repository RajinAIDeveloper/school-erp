"""
Phase 6c: analytics for the teachers who teach a subject, and for class teachers.

A subject teacher opens a paper for the sections they teach it in: how the marks spread, the
section and part averages, the pass rate where there are pass marks, and each student's
change since the last exam with anything to follow up. An exam still being marked is analysed
as a draft. A class teacher sees their section as a grid of students against subjects.
"""

from datetime import date
from decimal import Decimal

from django.test import Client

from academics.models import SubjectTeacher
from analytics.access import scope_for
from analytics.paper import allowed_sections, paper_analysis
from examinations.models import Exam, ExamSchedule, PaperComponent
from examinations.services import publish_exam, save_mark
from tests.test_progress import two_exams


def login(user):
    client = Client()
    client.force_login(user)
    return client


def paper_url(exam, subject, **extra):
    query = "&".join(f"{k}={v}" for k, v in {"exam": exam.pk, "subject": subject.pk, **extra}.items())
    return f"/analytics/paper/?{query}"


def test_a_papers_figures_are_worked_out_from_what_was_published(erp, board):
    two_exams(erp, board)
    bangla = board.subjects.bangla
    data = paper_analysis(board.exam, bangla, allowed_sections(board.exam, bangla, scope_for(erp.admin, erp.school)))
    assert data["sat"] == 2 and not data["draft"]
    assert data["average"] == Decimal("77.5") and data["median"] == Decimal("77.5")
    assert data["highest"] == Decimal("95.0") and data["lowest"] == Decimal("60.0")
    assert data["pass_rate"] == Decimal("100.0") and data["pass_mark"] == Decimal(66)
    bins = dict(data["bins"])
    assert bins["60–69"] == 1 and bins["90–100"] == 1
    assert sum(count for _letter, count in data["grades"]) == 2


def test_each_students_change_since_the_last_exam_is_flagged(erp, board):
    two_exams(erp, board)
    math = board.schedules["109"].subject
    data = paper_analysis(board.exam, math, allowed_sections(board.exam, math, scope_for(erp.admin, erp.school)))
    nabila = next(r for r in data["rows"] if r["enrollment"].pk == board.science.pk)
    assert nabila["before"] == Decimal("90.0") and nabila["change"] == Decimal("-20.0")
    assert "down since the last exam" in nabila["flags"]
    page = login(erp.admin).get(paper_url(board.exam, math)).content.decode()
    assert "down since the last exam" in page and "-20" in page


def test_a_pass_by_a_few_marks_is_flagged(erp, board):
    from tests.test_board_results import mark_everything

    mark_everything(erp, board, {(board.humanities.pk, "110"): 35})  # Geography: 2 over the pass mark
    publish_exam(board.exam, erp.admin)
    board.exam.refresh_from_db()
    geography = board.schedules["110"].subject
    data = paper_analysis(
        board.exam, geography, allowed_sections(board.exam, geography, scope_for(erp.admin, erp.school))
    )
    assert data["rows"][0]["flags"] == ["only just passed"]


def test_a_subject_teacher_opens_only_the_subjects_they_teach(erp, board):
    two_exams(erp, board)
    SubjectTeacher.objects.filter(teacher=erp.employee, section=board.section).exclude(
        subject=board.schedules["101"].subject
    ).delete()
    client = login(erp.teacher)
    assert client.get(paper_url(board.exam, board.subjects.bangla)).status_code == 200
    assert client.get(paper_url(board.exam, board.subjects.physics)).status_code == 404
    home = client.get(f"/analytics/?exam={board.exam.pk}").content.decode()
    assert "Bangla" in home and "Physics" not in home
    assert "Section grids are for class teachers" in home


def test_an_exam_being_marked_is_analysed_as_a_draft(erp):
    exam = Exam.objects.create(
        school=erp.school, academic_year=erp.year, name="Weekly test", grade_scale=erp.scale, end_date=date(2026, 9, 1)
    )
    schedule = ExamSchedule.objects.create(
        school=erp.school, exam=exam, class_level=erp.level, subject=erp.subject, full_marks=50, pass_marks=17
    )
    save_mark(user=erp.teacher, schedule=schedule, enrollment=erp.enrollment, score=Decimal(40))
    page = login(erp.teacher).get(paper_url(exam, erp.subject)).content.decode()
    assert "Draft: this exam is not published" in page and "80%" in page


def test_each_part_of_the_paper_has_its_own_average(erp):
    PaperComponent.objects.create(
        school=erp.school, schedule=erp.schedule, code="cq", name="Creative", full_marks=70, pass_marks=23, order=1
    )
    PaperComponent.objects.create(
        school=erp.school,
        schedule=erp.schedule,
        code="mcq",
        name="Multiple choice",
        full_marks=30,
        pass_marks=10,
        order=2,
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, components={"cq": "35", "mcq": "27"})
    publish_exam(erp.exam, erp.admin)
    erp.exam.refresh_from_db()
    data = paper_analysis(
        erp.exam, erp.subject, allowed_sections(erp.exam, erp.subject, scope_for(erp.teacher, erp.school))
    )
    assert data["parts"] == [("Creative", Decimal("50.0"), 1), ("Multiple choice", Decimal("90.0"), 1)]
    assert "Part by part" in login(erp.teacher).get(paper_url(erp.exam, erp.subject)).content.decode()


def test_a_class_teacher_sees_their_section_as_a_grid(erp, board):
    two_exams(erp, board)
    url = f"/analytics/section/?exam={board.exam.pk}&section={board.section.pk}"
    assert login(erp.teacher).get(url).status_code == 403  # not their section as class teacher
    board.section.class_teacher = erp.employee
    board.section.save()
    page = login(erp.teacher).get(url).content.decode()
    assert "Nabila" in page and "Rupa" in page and "Section average" in page and "Pass rate" in page
    exported = login(erp.teacher).get(url + "&format=csv").content.decode("utf-8-sig")
    assert exported.startswith("Roll,Student,") and "Nabila" in exported
    assert login(erp.admin).get(url).status_code == 200


def test_the_analytics_home_shows_each_person_what_they_may_open(erp, board):
    two_exams(erp, board)
    admin = login(erp.admin).get(f"/analytics/?exam={board.exam.pk}").content.decode()
    assert "Every subject in the school" in admin and "Physics" in admin and board.section.name in admin
    from students.models import StudentGuardian

    StudentGuardian.objects.create(student=board.science.student, guardian=erp.guardian, relation="mother")
    assert login(erp.parent).get("/analytics/")["Location"] == "/portal/progress/"
    assert login(erp.accountant).get("/analytics/").status_code == 403


def test_a_cambridge_paper_has_no_pass_rate_or_pass_flags(erp, igcse):
    for pupil, score in zip(igcse.pupils, (92, 34), strict=True):
        save_mark(
            user=erp.admin,
            schedule=igcse.papers["0625"],
            enrollment=pupil,
            components={"mcq": "30", "theory": "60", "practical": "30"},
        )
        save_mark(user=erp.admin, schedule=igcse.papers["0510"], enrollment=pupil, score=Decimal(score))
    publish_exam(igcse.exam, erp.admin)
    igcse.exam.refresh_from_db()
    english = igcse.papers["0510"].subject
    data = paper_analysis(igcse.exam, english, allowed_sections(igcse.exam, english, scope_for(erp.admin, erp.school)))
    assert data["pass_rate"] is None and data["pass_mark"] is None
    assert all("only just passed" not in r["flags"] and "failed" not in r["flags"] for r in data["rows"])
    page = login(erp.admin).get(paper_url(igcse.exam, english)).content.decode()
    assert "graded, not passed or failed" in page
