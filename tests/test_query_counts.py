"""
Result pages cost the same number of queries for a class of 30 as for a class of 3: nothing is
looked up once per student. Measured by growing the class and counting.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from academics.models import Subject
from attendance.models import StudentAttendance
from examinations.models import ExamSchedule
from examinations.services import save_mark
from students.models import Enrollment, Student


def grow(erp, count, start):
    """Add students to the section, each with marks in every paper and a day on the register."""
    schedules = list(ExamSchedule.objects.filter(exam=erp.exam))
    for roll in range(start, start + count):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"Q{roll}",
            first_name=f"Pupil{roll}",
            gender="F",
            date_of_birth=date(2016, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        enrollment = Enrollment.objects.create(
            school=erp.school,
            student=student,
            academic_year=erp.year,
            class_level=erp.level,
            section=erp.section,
            roll_number=roll,
        )
        StudentAttendance.objects.create(
            school=erp.school, enrollment=enrollment, date=date(2026, 9, 21), status="present"
        )
        for schedule in schedules:
            save_mark(user=erp.admin, schedule=schedule, enrollment=enrollment, score=Decimal(60 + roll % 30))


@pytest.fixture
def classroom(erp):
    for code in ("ENG", "SCI", "BAN"):
        subject = Subject.objects.create(school=erp.school, name=code.title(), code=code)
        ExamSchedule.objects.create(
            school=erp.school, exam=erp.exam, class_level=erp.level, subject=subject, full_marks=100, pass_marks=33
        )
    for schedule in ExamSchedule.objects.filter(exam=erp.exam):
        save_mark(user=erp.admin, schedule=schedule, enrollment=erp.enrollment, score=Decimal(75))
    return erp


def queries(client, url):
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, url
    return len(captured)


@pytest.mark.parametrize(
    "page",
    [
        lambda erp: f"/exams/{erp.exam.pk}/",
        lambda erp: f"/exams/results/?exam={erp.exam.pk}&class_level={erp.level.pk}",
        lambda erp: f"/exams/{erp.exam.pk}/report/{erp.student.pk}/",
        lambda erp: f"/exams/report-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}",
        lambda erp: f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}",
    ],
    ids=["exam page", "results sheet", "report card", "section cards PDF", "mark entry"],
)
def test_a_bigger_class_costs_no_more_queries(classroom, page):
    client = Client()
    client.force_login(classroom.admin)
    url = page(classroom)
    grow(classroom, 2, start=2)
    small = queries(client, url)
    grow(classroom, 25, start=4)
    large = queries(client, url)
    assert large == small, f"{large - small} more queries for 25 more students"
