"""
Phase 6b: a student's progress, for the family and for staff.

Every published exam in order, each subject against the class average and highest, what is
going well and what needs attention, attendance by month, and a printable report. A family
sees only its own child and never another child's name; a subject teacher sees only the
subjects they teach; positions appear only where the school shows them.
"""

from datetime import date
from decimal import Decimal

from django.test import Client

from analytics.access import scope_for
from analytics.progress import student_progress
from core.charts import bullet_chart
from examinations.models import Exam, ExamSchedule
from examinations.services import publish_exam, save_mark
from examinations.subjects import takes_paper
from students.models import StudentGuardian
from tests.test_board_results import mark_everything


def login(user):
    client = Client()
    client.force_login(user)
    return client


def earlier_exam(erp, board, scores):
    """A First Term exam before the board's Half Yearly, with the same papers."""
    exam = Exam.objects.create(
        school=erp.school,
        academic_year=erp.year,
        name="First Term",
        grade_scale=erp.scale,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 10),
    )
    for code, source in board.schedules.items():
        schedule = ExamSchedule.objects.create(
            school=erp.school, exam=exam, class_level=board.level, subject=source.subject, full_marks=100, pass_marks=33
        )
        for enrollment in (board.science, board.humanities):
            if takes_paper(enrollment, schedule):
                save_mark(
                    user=erp.admin,
                    schedule=schedule,
                    enrollment=enrollment,
                    score=Decimal(scores.get((enrollment.pk, code), 80)),
                )
    publish_exam(exam, erp.admin)
    return exam


def two_exams(erp, board):
    """Nabila is strong in Bangla and has slipped in General Math; Rupa is behind in Bangla."""
    s, h = board.science.pk, board.humanities.pk
    earlier_exam(erp, board, {(s, "109"): 90, (h, "109"): 72})
    board.exam.start_date, board.exam.end_date = date(2026, 6, 1), date(2026, 6, 10)
    board.exam.save()
    mark_everything(
        erp,
        board,
        {(s, "101"): 95, (s, "102"): 95, (h, "101"): 60, (h, "102"): 60, (s, "109"): 70, (h, "109"): 72},
    )
    publish_exam(board.exam, erp.admin)
    board.exam.refresh_from_db()


def family(erp, board):
    StudentGuardian.objects.create(student=board.science.student, guardian=erp.guardian, relation="mother")
    return login(erp.parent)


def test_progress_follows_the_exams_and_compares_with_the_class(erp, board):
    two_exams(erp, board)
    data = student_progress(board.science.student, scope_for(erp.admin, erp.school))
    assert [row["exam"].name for row in data["exams"]] == ["First Term", "Half Yearly"]
    bangla = next(row for row in data["latest_subjects"] if row["subject"] == "Bangla")
    assert bangla["percent"] == Decimal("95.0")
    assert bangla["average"] == Decimal("77.5") and bangla["highest"] == Decimal("95.0")
    assert "Bangla" in data["strengths"]
    assert ("General Math", "down since the last exam") in data["attention"]
    math = dict(data["trends"]["subjects"])["General Math"]
    assert math == [Decimal("90.0"), Decimal("70.0")]
    rupa = student_progress(board.humanities.student, scope_for(erp.admin, erp.school))
    assert ("Bangla", "below the class average") in rupa["attention"]


def test_a_family_sees_its_own_child_against_the_class_and_no_other_name(erp, board):
    two_exams(erp, board)
    client = family(erp, board)
    page = client.get(f"/portal/progress/?student={board.science.student_id}").content.decode()
    assert "Nabila" in page and "Rupa" not in page
    assert "Class average" in page and "Class highest" in page and "<svg" in page
    assert "Going well" in page and "down since the last exam" in page
    # Another family's child is not theirs to open, on either page.
    assert client.get(f"/portal/progress/?student={board.humanities.student_id}").status_code == 404
    assert client.get(f"/analytics/student/{board.humanities.student_id}/").status_code == 404
    report = client.get(f"/portal/progress/?student={board.science.student_id}&format=pdf")
    assert report.status_code == 200 and report["Content-Type"] == "application/pdf"


def test_a_subject_teacher_sees_only_their_subject(erp, board):
    two_exams(erp, board)
    from academics.models import SubjectTeacher

    SubjectTeacher.objects.filter(teacher=erp.employee, section=board.section).exclude(
        subject=board.schedules["101"].subject
    ).delete()
    page = login(erp.teacher).get(f"/analytics/student/{board.science.student_id}/").content.decode()
    assert "Bangla" in page and "Physics" not in page and "General Math" not in page
    assert "Every published exam" not in page  # the overall result belongs to the class teacher
    # A teacher with nothing in that section sees nothing of the student.
    SubjectTeacher.objects.filter(teacher=erp.employee, section=board.section).delete()
    assert login(erp.teacher).get(f"/analytics/student/{board.science.student_id}/").status_code == 404


def test_positions_show_where_the_school_shows_them(erp, board):
    two_exams(erp, board)
    national = login(erp.admin).get(f"/analytics/student/{board.science.student_id}/").content.decode()
    assert "in the section" in national


def test_positions_stay_hidden_where_the_school_hides_them(erp, igcse):
    for pupil, score in zip(igcse.pupils, (92, 55), strict=True):
        save_mark(
            user=erp.admin,
            schedule=igcse.papers["0625"],
            enrollment=pupil,
            components={"mcq": "30", "theory": "60", "practical": "30"},
        )
        save_mark(user=erp.admin, schedule=igcse.papers["0510"], enrollment=pupil, score=Decimal(score))
    publish_exam(igcse.exam, erp.admin)
    cambridge = login(erp.admin).get(f"/analytics/student/{igcse.pupils[0].student_id}/").content.decode()
    assert "Positions are not shown for this exam" in cambridge
    # No GPA or pass/fail under Cambridge: the grades themselves, as the card gives them.
    from examinations.models import ExamResultFact

    headline = ExamResultFact.objects.get(exam=igcse.exam, enrollment=igcse.pupils[0]).headline
    assert headline and headline in cambridge and "No subject failed" not in cambridge


def test_progress_reads_in_bangla_and_prints(erp, board):
    two_exams(erp, board)
    client = family(erp, board)
    erp.parent.language = "bn"
    erp.parent.save()
    page = client.get(f"/portal/progress/?student={board.science.student_id}").content.decode()
    assert "অগ্রগতি" in page and "শ্রেণির গড়" in page and "ভালো করছে" in page
    staff = login(erp.admin).get(f"/analytics/student/{board.science.student_id}/?format=pdf")
    assert staff.status_code == 200 and staff.content.startswith(b"%PDF")


def test_nothing_published_means_an_empty_page_not_an_error(erp):
    page = login(erp.parent).get(f"/portal/progress/?student={erp.student.pk}").content.decode()
    assert "No published results yet" in page


def test_chart_labels_are_escaped():
    svg = bullet_chart([("<script>alert(1)</script>", Decimal(50), Decimal(40), Decimal(90))], "A <title>")
    assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_the_family_page_opens_on_a_child_with_published_results(erp, board):
    two_exams(erp, board)
    client = family(erp, board)  # this guardian also has Ayesha, with nothing published
    response = client.get("/portal/progress/")
    assert response.context["selected"] == board.science.student
