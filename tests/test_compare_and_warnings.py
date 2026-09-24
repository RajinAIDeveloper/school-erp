"""
Proof and insight.

Recompute and compare: the old software's results for an exam, checked against this system's,
with every mismatch named and nothing saved. Early warning: the students in a section to talk
to now, from the same registers and published results everything else uses.
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from attendance.models import StudentAttendance
from examinations.compare import compare
from examinations.models import Exam, ExamSchedule, Mark
from examinations.services import build_result_sheet, publish_exam, save_mark
from tests.test_board_results import mark_everything


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ------------------------------------------------------------------ recompute and compare


def published_file(rows, change=None):
    """What an old program would have published for this class, as CSV rows, optionally with one change."""
    lines = [["Student ID", "Subject", "Marks", "Grade", "GPA"]]
    for row in rows:
        for unit in row["subjects"]:
            lines.append([row["student_code"], unit["name"], unit["score"], unit["letter"], row["gpa"]])
    if change:
        change(lines)
    return lines


def upload(lines):
    buffer = io.StringIO()
    csv.writer(buffer).writerows(lines)
    return SimpleUploadedFile("old.csv", buffer.getvalue().encode("utf-8"))


def test_a_file_that_agrees_has_no_differences(erp, board):
    mark_everything(erp, board)
    rows = build_result_sheet(board.exam, board.level)["rows"]
    differences, summary = compare(rows, [[str(c) for c in line] for line in published_file(rows)])
    assert differences == []
    assert summary["students"] == 2 and summary["compared"] > 10


def test_every_mismatch_is_named(erp, board):
    mark_everything(erp, board, {(board.science.pk, "136"): 70})
    rows = build_result_sheet(board.exam, board.level)["rows"]

    def disagree(lines):
        for line in lines:
            if line[0] == "C9-1" and line[1] == "Physics":
                line[2] = "72"  # the old program had 72
            if line[0] == "C9-1":
                line[4] = "4.94"
        lines.append(["NOBODY", "Physics", "50", "C", ""])
        lines.append(["C9-1", "Geography", "60", "A-", ""])  # a Science student does not sit Geography

    lines = [[str(c) for c in line] for line in published_file(rows, disagree)]
    differences, summary = compare(rows, lines)
    found = {(d["student"], d["subject"], d["what"]) for d in differences}
    assert ("Nabila", "Physics", "Marks") in found
    assert ("Nabila", "GPA", "GPA") in found
    assert ("NOBODY", "Physics", "Student") in found
    assert ("Nabila", "Geography", "Subject") in found
    assert summary["differences"] == 4


def test_subjects_match_by_code_or_name_and_absences_agree(erp, board):
    mark_everything(erp, board)
    save_mark(
        user=erp.admin, schedule=board.schedules["136"], enrollment=board.science, absent=True, expected_version=1
    )
    rows = build_result_sheet(board.exam, board.level)["rows"]
    lines = [
        ["Student ID", "Subject", "Marks"],
        ["C9-1", "101", "85"],  # Bangla 1st paper, by its code
        ["C9-1", "Bangla", "170"],  # both papers together, by the subject's name
        ["C9-1", "Physics", "ABS"],
    ]
    differences, _summary = compare(rows, lines)
    assert differences == []


def test_the_compare_screen_checks_a_file_and_saves_nothing(erp, board):
    mark_everything(erp, board)
    rows = build_result_sheet(board.exam, board.level)["rows"]
    client = login(erp.admin)
    form = {"exam": board.exam.pk, "class_level": board.level.pk}
    marks_before = list(Mark.objects.values_list("pk", "marks_obtained"))

    page = client.post(
        "/exams/results/compare/", {**form, "action": "compare", "file": upload(published_file(rows))}
    ).content.decode()
    assert "Every mark, grade and GPA in the file matches." in page

    def disagree(lines):
        lines[1][2] = "1"

    page = client.post(
        "/exams/results/compare/", {**form, "action": "compare", "file": upload(published_file(rows, disagree))}
    ).content.decode()
    assert "1 difference" in page
    assert list(Mark.objects.values_list("pk", "marks_obtained")) == marks_before

    template = client.post("/exams/results/compare/", {**form, "action": "template_csv"}).content.decode("utf-8-sig")
    assert "Student ID,Student,Subject,Code,Marks,Grade,GPA" in template and "C9-1,Nabila" in template


def test_only_managers_compare(erp, board):
    assert login(erp.teacher).get("/exams/results/compare/").status_code == 403


# ------------------------------------------------------------------ early warning


def second_exam(erp, score):
    """A later published exam in the same subject, with this score for Ayesha."""
    exam = Exam.objects.create(
        school=erp.school,
        academic_year=erp.year,
        name="Term 2",
        grade_scale=erp.scale,
        end_date=timezone.localdate() - timedelta(days=5),
    )
    schedule = ExamSchedule.objects.create(
        school=erp.school, exam=exam, class_level=erp.level, subject=erp.subject, full_marks=100, pass_marks=33
    )
    save_mark(user=erp.teacher, schedule=schedule, enrollment=erp.enrollment, score=Decimal(score))
    publish_exam(exam, erp.admin)
    return exam


def first_exam(erp, score=80):
    erp.exam.end_date = timezone.localdate() - timedelta(days=60)
    erp.exam.save()
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(score))
    publish_exam(erp.exam, erp.admin)


def warnings_page(client, erp, **extra):
    query = "&".join(f"{k}={v}" for k, v in {"section": erp.section.pk, **extra}.items())
    return client.get(f"/reports/early-warning/?{query}").content.decode()


def test_a_drop_since_the_last_exam_and_a_failed_subject_are_flagged(erp):
    first_exam(erp, 80)
    second_exam(erp, 30)
    body = warnings_page(login(erp.teacher), erp)
    assert "Ayesha" in body
    assert "Down 50 points since Term 1" in body
    assert "Failed in Term 2: Math" in body


def test_an_only_just_pass_and_low_attendance_are_flagged(erp):
    first_exam(erp, 36)
    for offset, status in enumerate(["present", "absent", "absent", "present"]):
        StudentAttendance.objects.create(
            school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 1) + timedelta(days=offset), status=status
        )
    body = warnings_page(login(erp.teacher), erp)
    assert "Attendance 50% this year" in body
    assert "Only just passed: Math (passed by 3)" in body
    # A narrower margin and a lower threshold clear both.
    assert "1 of 1" not in warnings_page(login(erp.teacher), erp, attendance_below=40, margin=2)


def test_a_student_doing_well_is_not_listed(erp):
    first_exam(erp, 80)
    second_exam(erp, 78)
    body = warnings_page(login(erp.teacher), erp)
    assert "0 of 1 student listed" in body and "No student in this section needs attention" in body


def test_teachers_see_their_own_sections_and_the_list_exports(erp):
    first_exam(erp, 80)
    second_exam(erp, 30)
    teacher = login(erp.teacher)
    other = teacher.get(f"/reports/early-warning/?section={erp.other_section.pk}").content.decode()
    assert "Select a valid choice" in other
    exported = teacher.get(f"/reports/early-warning/?section={erp.section.pk}&format=csv").content.decode("utf-8-sig")
    assert "Roll,Student,Attendance,Latest result,Change,Concerns" in exported and "Ayesha" in exported
    assert "Early warning" in teacher.get("/reports/").content.decode()
    assert login(erp.parent).get("/reports/early-warning/").status_code == 403
