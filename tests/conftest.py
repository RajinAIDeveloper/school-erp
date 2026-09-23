from datetime import date
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command

from academics.models import AcademicYear, ClassLevel, ClassSubject, Section, Subject, SubjectTeacher
from core.models import AssessmentSystem, School
from employees.models import Employee
from examinations.models import Exam, ExamSchedule, ensure_default_grade_scale
from fees.models import FeeCategory, FeeStructure
from finance.models import ensure_default_accounts
from students.models import Enrollment, Guardian, Student, StudentGuardian
from users.models import User


@pytest.fixture(autouse=True)
def fast_test_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def erp(db):
    call_command("setup_roles", verbosity=0)
    school = School.objects.create(name="Test School", slug="test", weekend_days="5,6")
    other = School.objects.create(name="Other School", slug="other")
    admin = User.objects.create_user(username="admin", school=school, password="Test-pass-9842")
    admin.groups.add(Group.objects.get(name="Administrator"))
    teacher = User.objects.create_user(username="teacher", school=school, password="Test-pass-9842")
    teacher.groups.add(Group.objects.get(name="Teacher"))
    parent = User.objects.create_user(username="parent", school=school, password="Test-pass-9842")
    parent.groups.add(Group.objects.get(name="Guardian"))
    staff = User.objects.create_user(username="staff", school=school, password="Test-pass-9842")
    staff.groups.add(Group.objects.get(name="Staff"))
    accountant = User.objects.create_user(username="accountant", school=school, password="Test-pass-9842")
    accountant.groups.add(Group.objects.get(name="Accountant"))
    year = AcademicYear.objects.create(
        school=school, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), is_current=True
    )
    level = ClassLevel.objects.create(school=school, name="Class 1", order=1)
    section = Section.objects.create(school=school, class_level=level, name="A")
    other_section = Section.objects.create(school=school, class_level=level, name="B")
    subject = Subject.objects.create(school=school, name="Math", code="MATH")
    employee = Employee.objects.create(
        school=school,
        user=teacher,
        employee_id="T1",
        first_name="Teacher",
        gender="M",
        joining_date=date(2025, 1, 1),
        phone="01712345678",
    )
    SubjectTeacher.objects.create(school=school, teacher=employee, section=section, subject=subject, academic_year=year)
    student = Student.objects.create(
        school=school,
        student_id="S1",
        first_name="Ayesha",
        gender="F",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    enrollment = Enrollment.objects.create(
        school=school, student=student, academic_year=year, class_level=level, section=section, roll_number=1
    )
    guardian = Guardian.objects.create(school=school, user=parent, full_name="Parent", phone="01712345679")
    StudentGuardian.objects.create(student=student, guardian=guardian, relation="mother", is_primary=True)
    scale = ensure_default_grade_scale(school)
    exam = Exam.objects.create(school=school, academic_year=year, name="Term 1", grade_scale=scale)
    schedule = ExamSchedule.objects.create(
        school=school, exam=exam, class_level=level, subject=subject, full_marks=100, pass_marks=33
    )
    category = FeeCategory.objects.create(school=school, name="Tuition")
    structure = FeeStructure.objects.create(
        school=school, academic_year=year, class_level=level, category=category, amount=1000, frequency="monthly"
    )
    ensure_default_accounts(school)
    return SimpleNamespace(**{k: v for k, v in locals().copy().items() if k not in ("db",)})


@pytest.fixture
def admin_client(client, erp):
    client.force_login(erp.admin)
    return client


@pytest.fixture
def invoice(erp):
    from fees.models import FeeInvoice
    from fees.services import generate_invoices

    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    return FeeInvoice.objects.get()


@pytest.fixture
def board(erp):
    """
    A Class 9 under the national curriculum: two groups, a combined Bangla, religion papers,
    and a 4th subject. One Muslim Science student and one Hindu Humanities student.
    """
    school, year = erp.school, erp.year
    # Board rules are opt-in: the school's default is its own rules, and this class chooses
    # the national curriculum explicitly.
    level = ClassLevel.objects.create(
        school=school, name="Class 9", order=9, assessment_system=AssessmentSystem.NATIONAL
    )
    section = Section.objects.create(school=school, class_level=level, name="A", shift="morning", version="bangla")

    def subject(name, code, **extra):
        return Subject.objects.create(school=school, name=name, code=code, **extra)

    bangla = subject("Bangla", "")
    b1 = subject("Bangla 1st paper", "101", combines_into=bangla)
    b2 = subject("Bangla 2nd paper", "102", combines_into=bangla)
    math = subject("General Math", "109")
    islam = subject("Islam and Moral Education", "111", religion="islam")
    hindu = subject("Hindu Religion and Moral Education", "112", religion="hinduism")
    physics = subject("Physics", "136")
    geography = subject("Geography and Environment", "110")
    higher_math = subject("Higher Math", "126")
    agriculture = subject("Agriculture Studies", "134")

    def plan(subj, group="", kind=ClassSubject.Kind.COMPULSORY):
        ClassSubject.objects.create(
            school=school, academic_year=year, class_level=level, subject=subj, group=group, kind=kind
        )

    for subj in (b1, b2, math, islam, hindu):
        plan(subj)
    plan(physics, "science")
    plan(geography, "humanities")
    for subj in (higher_math, agriculture):
        plan(subj, "", ClassSubject.Kind.CHOICE)

    exam = Exam.objects.create(school=school, academic_year=year, name="Half Yearly", grade_scale=erp.scale)
    schedules = {}
    for subj in (b1, b2, math, islam, hindu, physics, geography, higher_math, agriculture):
        schedules[subj.code] = ExamSchedule.objects.create(
            school=school, exam=exam, class_level=level, subject=subj, full_marks=100, pass_marks=33
        )

    def student(sid, name, religion, group, fourth, roll, guardian_phone):
        person = Student.objects.create(
            school=school,
            student_id=sid,
            first_name=name,
            gender="F",
            religion=religion,
            date_of_birth=date(2011, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        enrollment = Enrollment.objects.create(
            school=school,
            student=person,
            academic_year=year,
            class_level=level,
            section=section,
            roll_number=roll,
            group=group,
            fourth_subject=fourth,
        )
        guardian = Guardian.objects.create(school=school, full_name=f"{name}'s parent", phone=guardian_phone)
        StudentGuardian.objects.create(student=person, guardian=guardian, relation="mother", is_primary=True)
        return enrollment

    science = student("C9-1", "Nabila", "islam", "science", higher_math, 1, "01712340001")
    humanities = student("C9-2", "Rupa", "hinduism", "humanities", agriculture, 2, "01712340002")

    teacher = erp.employee
    for subj in (b1, b2, math, islam, hindu, physics, geography, higher_math, agriculture):
        SubjectTeacher.objects.create(school=school, academic_year=year, section=section, subject=subj, teacher=teacher)

    return SimpleNamespace(
        level=level,
        section=section,
        exam=exam,
        schedules=schedules,
        science=science,
        humanities=humanities,
        subjects=SimpleNamespace(bangla=bangla, higher_math=higher_math, agriculture=agriculture, physics=physics),
    )
