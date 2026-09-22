from datetime import date
from types import SimpleNamespace
import pytest
from django.core.management import call_command
from django.contrib.auth.models import Group
from core.models import School
from users.models import User
from academics.models import AcademicYear,ClassLevel,Section,Subject,SubjectTeacher
from students.models import Student,Enrollment,Guardian,StudentGuardian
from employees.models import Employee
from examinations.models import Exam,ExamSchedule,ensure_default_grade_scale
from fees.models import FeeCategory,FeeStructure
from finance.models import ensure_default_accounts

@pytest.fixture(autouse=True)
def fast_test_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

@pytest.fixture
def erp(db):
    call_command("setup_roles",verbosity=0)
    school=School.objects.create(name="Test School",slug="test",weekend_days="5,6")
    other=School.objects.create(name="Other School",slug="other")
    admin=User.objects.create_user(username="admin",school=school,password="Test-pass-9842")
    admin.groups.add(Group.objects.get(name="Administrator"))
    teacher=User.objects.create_user(username="teacher",school=school,password="Test-pass-9842")
    teacher.groups.add(Group.objects.get(name="Teacher"))
    parent=User.objects.create_user(username="parent",school=school,password="Test-pass-9842")
    parent.groups.add(Group.objects.get(name="Guardian"))
    staff=User.objects.create_user(username="staff",school=school,password="Test-pass-9842")
    staff.groups.add(Group.objects.get(name="Staff"))
    accountant=User.objects.create_user(username="accountant",school=school,password="Test-pass-9842")
    accountant.groups.add(Group.objects.get(name="Accountant"))
    year=AcademicYear.objects.create(school=school,name="2026",start_date=date(2026,1,1),end_date=date(2026,12,31),is_current=True)
    level=ClassLevel.objects.create(school=school,name="Class 1",order=1)
    section=Section.objects.create(school=school,class_level=level,name="A")
    other_section=Section.objects.create(school=school,class_level=level,name="B")
    subject=Subject.objects.create(school=school,name="Math",code="MATH")
    employee=Employee.objects.create(school=school,user=teacher,employee_id="T1",first_name="Teacher",gender="M",joining_date=date(2025,1,1),phone="01712345678")
    SubjectTeacher.objects.create(school=school,teacher=employee,section=section,subject=subject,academic_year=year)
    student=Student.objects.create(school=school,student_id="S1",first_name="Ayesha",gender="F",date_of_birth=date(2016,1,1),admission_date=date(2026,1,1))
    enrollment=Enrollment.objects.create(school=school,student=student,academic_year=year,class_level=level,section=section,roll_number=1)
    guardian=Guardian.objects.create(school=school,user=parent,full_name="Parent",phone="01712345679")
    StudentGuardian.objects.create(student=student,guardian=guardian,relation="mother",is_primary=True)
    scale=ensure_default_grade_scale(school)
    exam=Exam.objects.create(school=school,academic_year=year,name="Term 1",grade_scale=scale)
    schedule=ExamSchedule.objects.create(school=school,exam=exam,class_level=level,subject=subject,full_marks=100,pass_marks=33)
    category=FeeCategory.objects.create(school=school,name="Tuition")
    structure=FeeStructure.objects.create(school=school,academic_year=year,class_level=level,category=category,amount=1000,frequency="monthly")
    ensure_default_accounts(school)
    return SimpleNamespace(**{k:v for k,v in locals().copy().items() if k not in ("db",)})

@pytest.fixture
def admin_client(client,erp):
    client.force_login(erp.admin)
    return client

@pytest.fixture
def invoice(erp):
    from fees.services import generate_invoices
    from fees.models import FeeInvoice
    generate_invoices(school=erp.school,user=erp.admin,academic_year=erp.year,class_level=erp.level,
                      month=9,issue_date=date(2026,9,1),due_date=date(2026,9,10))
    return FeeInvoice.objects.get()
