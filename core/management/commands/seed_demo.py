from datetime import date, time

from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academics.models import AcademicYear, ClassLevel, Section, Subject, SubjectTeacher
from attendance.models import AttendanceStatus, LeaveRequest, LeaveType, StaffAttendance, StudentAttendance
from core.models import School
from downloads.models import Audience, DownloadCategory, DownloadItem
from employees.models import Department, Designation, Employee
from examinations.models import Exam, ExamSchedule, Mark, ensure_default_grade_scale
from fees.models import FeeCategory, FeeStructure
from fees.services import generate_invoices
from finance.models import ensure_default_accounts
from holidays.models import Holiday
from messaging.models import SMSTemplate
from students.models import Enrollment, Guardian, Student, StudentGuardian
from timetable.models import Period, Room, RoutineSlot
from users.models import User

DEMO_ROLES = (
    ("demo_admin", "Administrator", "Demo", "Administrator"),
    ("demo_principal", "Principal", "Demo", "Principal"),
    ("demo_vice", "Vice Principal", "Demo", "Vice Principal"),
    ("demo_accountant", "Accountant", "Demo", "Accountant"),
    ("demo_teacher", "Teacher", "Demo", "Teacher"),
    ("demo_staff", "Staff", "Demo", "Staff"),
    ("demo_student", "Student", "Demo", "Student"),
    ("demo_guardian", "Guardian", "Demo", "Guardian"),
)


def ensure_demo_user(*, school, username, role, first_name, last_name, password, reset_password=False):
    """Create one role account without changing a pre-existing password by default."""
    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            "school": school,
            "first_name": first_name,
            "last_name": last_name,
            "email": f"{username}@demo.school",
            "is_active": True,
        },
    )
    if user.school_id not in (None, school.pk):
        raise CommandError(f"User {username!r} already belongs to another school.")
    updates = []
    if user.school_id is None:
        user.school = school
        updates.append("school")
    if not user.is_active:
        user.is_active = True
        updates.append("is_active")
    if created or reset_password:
        user.set_password(password)
        updates.append("password")
    if updates:
        user.save(update_fields=sorted(set(updates)))
    user.groups.add(Group.objects.get(name=role))
    return user


class Command(BaseCommand):
    help = "Create idempotent demonstration school data and one login for each ERP role."

    def add_arguments(self, parser):
        parser.add_argument("--school-name", default="School ERP Demo")
        parser.add_argument("--slug", default="demo-school")
        parser.add_argument("--admin-username", default=None)
        parser.add_argument("--demo-password", default="DemoPass!2026")
        parser.add_argument(
            "--no-programmes",
            action="store_true",
            help="Skip the Cambridge IGCSE, IB Diploma and national Class 9 demonstration classes.",
        )
        parser.add_argument(
            "--reset-demo-password",
            action="store_true",
            help="Reset passwords for the demo_* accounts (never changes other users).",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        call_command("setup_roles")
        school, _ = School.objects.get_or_create(
            slug=opts["slug"],
            defaults={"name": opts["school_name"], "short_name": "Demo School", "phone": "01700000000"},
        )
        demo_users = {
            username: ensure_demo_user(
                school=school,
                username=username,
                role=role,
                first_name=first_name,
                last_name=last_name,
                password=opts["demo_password"],
                reset_password=opts["reset_demo_password"],
            )
            for username, role, first_name, last_name in DEMO_ROLES
        }
        demo_admin = demo_users["demo_admin"]
        demo_teacher = demo_users["demo_teacher"]
        demo_student_user = demo_users["demo_student"]
        demo_guardian_user = demo_users["demo_guardian"]
        year, _ = AcademicYear.objects.get_or_create(
            school=school,
            name="2026",
            defaults={"start_date": date(2026, 1, 1), "end_date": date(2026, 12, 31), "is_current": True},
        )
        department, _ = Department.objects.get_or_create(school=school, name="Academics")
        designation, _ = Designation.objects.get_or_create(school=school, name="Assistant Teacher")
        teacher, _ = Employee.objects.get_or_create(
            school=school,
            employee_id="DEMO-T1",
            defaults={
                "first_name": "Demo",
                "last_name": "Teacher",
                "gender": "F",
                "phone": "01700000000",
                "joining_date": date(2026, 1, 1),
                "department": department,
                "designation": designation,
                "basic_salary": 25000,
            },
        )
        if teacher.user_id in (None, demo_teacher.pk):
            teacher.user = demo_teacher
            teacher.save(update_fields=["user"])
        staff, _ = Employee.objects.get_or_create(
            school=school,
            employee_id="DEMO-S1",
            defaults={
                "employee_type": Employee.Type.STAFF,
                "first_name": "Demo",
                "last_name": "Staff",
                "gender": "M",
                "phone": "01700000001",
                "joining_date": date(2026, 1, 1),
                "department": department,
                "designation": designation,
                "basic_salary": 18000,
            },
        )
        if staff.user_id in (None, demo_users["demo_staff"].pk):
            staff.user = demo_users["demo_staff"]
            staff.save(update_fields=["user"])
        level, _ = ClassLevel.objects.get_or_create(school=school, name="Class 1", defaults={"order": 1})
        section, _ = Section.objects.get_or_create(
            school=school, class_level=level, name="A", defaults={"class_teacher": teacher}
        )
        subject, _ = Subject.objects.get_or_create(school=school, name="Mathematics", defaults={"code": "MATH"})
        subject.class_levels.add(level)
        SubjectTeacher.objects.get_or_create(
            school=school, academic_year=year, section=section, subject=subject, defaults={"teacher": teacher}
        )
        enrollments = []
        demo_student = None
        demo_guardian = None
        for n in range(1, 6):
            student, _ = Student.objects.get_or_create(
                school=school,
                student_id=f"DEMO-{n:03}",
                defaults={
                    "first_name": f"Demo Student {n}",
                    "gender": "F" if n % 2 else "M",
                    "date_of_birth": date(2018, 1, n),
                    "admission_date": date(2026, 1, 1),
                },
            )
            Enrollment.objects.get_or_create(
                school=school,
                student=student,
                academic_year=year,
                defaults={"section": section, "class_level": level, "roll_number": n},
            )
            enrollment = Enrollment.objects.get(school=school, student=student, academic_year=year)
            enrollments.append(enrollment)
            guardian, _ = Guardian.objects.get_or_create(
                school=school, full_name=f"Demo Guardian {n}", defaults={"phone": "01700000000"}
            )
            StudentGuardian.objects.get_or_create(
                student=student, guardian=guardian, defaults={"relation": "mother", "is_primary": True}
            )
            if n == 1:
                demo_student = student
                demo_guardian = guardian
        if demo_student.user_id in (None, demo_student_user.pk):
            demo_student.user = demo_student_user
            demo_student.save(update_fields=["user"])
        if demo_guardian.user_id in (None, demo_guardian_user.pk):
            demo_guardian.user = demo_guardian_user
            demo_guardian.save(update_fields=["user"])
        ensure_default_accounts(school)
        category, _ = FeeCategory.objects.get_or_create(school=school, name="Tuition")
        FeeStructure.objects.get_or_create(
            school=school, academic_year=year, class_level=level, category=category, defaults={"amount": 1000}
        )
        scale = ensure_default_grade_scale(school)
        exam, _ = Exam.objects.get_or_create(
            school=school,
            academic_year=year,
            name="First Term",
            defaults={"grade_scale": scale, "start_date": date(2026, 10, 1), "end_date": date(2026, 10, 5)},
        )
        schedule, _ = ExamSchedule.objects.get_or_create(
            school=school,
            exam=exam,
            class_level=level,
            subject=subject,
            defaults={
                "date": date(2026, 10, 1),
                "start_time": time(10),
                "end_time": time(12),
                "full_marks": 100,
                "pass_marks": 33,
            },
        )
        period, _ = Period.objects.get_or_create(
            school=school, order=1, defaults={"name": "Period 1", "start_time": time(9), "end_time": time(9, 45)}
        )
        room, _ = Room.objects.get_or_create(school=school, name="Room 101")
        RoutineSlot.objects.get_or_create(
            school=school,
            academic_year=year,
            section=section,
            weekday=1,
            period=period,
            defaults={"subject": subject, "teacher": teacher, "room": room},
        )
        Holiday.objects.get_or_create(
            school=school,
            name="Demo winter holiday",
            defaults={"start_date": date(2026, 12, 20), "end_date": date(2026, 12, 25)},
        )
        DownloadCategory.objects.get_or_create(school=school, name="Learning materials")
        SMSTemplate.objects.get_or_create(
            school=school,
            name="Fee reminder",
            defaults={"body": "Dear guardian, {student} has outstanding fees of {amount}. Please contact {school}."},
        )
        attendance_day = date(2026, 9, 21)
        student_statuses = [
            AttendanceStatus.ABSENT,
            AttendanceStatus.LATE,
            AttendanceStatus.PRESENT,
            AttendanceStatus.PRESENT,
            AttendanceStatus.PRESENT,
        ]
        for enrollment, status in zip(enrollments, student_statuses, strict=True):
            StudentAttendance.objects.get_or_create(
                school=school,
                enrollment=enrollment,
                date=attendance_day,
                defaults={"status": status, "recorded_by": demo_teacher},
            )
        for employee in (teacher, staff):
            StaffAttendance.objects.get_or_create(
                school=school,
                employee=employee,
                date=attendance_day,
                defaults={"status": AttendanceStatus.PRESENT, "recorded_by": demo_admin},
            )
        for enrollment in enrollments:
            Mark.objects.get_or_create(
                school=school,
                schedule=schedule,
                enrollment=enrollment,
                defaults={"marks_obtained": 70 + enrollment.roll_number, "entered_by": demo_teacher},
            )
        leave_type, _ = LeaveType.objects.get_or_create(school=school, name="Casual", defaults={"days_per_year": 10})
        LeaveRequest.objects.get_or_create(
            school=school,
            employee=staff,
            leave_type=leave_type,
            start_date=date(2026, 9, 28),
            end_date=date(2026, 9, 29),
            defaults={"reason": "Demo leave request for approval workflow."},
        )
        generate_invoices(
            school=school,
            user=demo_admin,
            academic_year=year,
            class_level=level,
            month=9,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 30),
        )
        download_category = DownloadCategory.objects.get(school=school, name="Learning materials")
        DownloadItem.objects.get_or_create(
            school=school,
            title="Demo mathematics study note",
            defaults={
                "category": download_category,
                "description": "A small file for testing the protected download workflow.",
                "file": SimpleUploadedFile("demo-mathematics.txt", b"School ERP demo study note\n"),
                "audience": Audience.STUDENTS,
                "uploaded_by": demo_admin,
            },
        )
        if not opts["no_programmes"]:
            from core.demo_programmes import seed_programmes

            seed_programmes(
                school=school,
                year=year,
                admin=demo_admin,
                principal=demo_users["demo_principal"],
                teacher=teacher,
                demo_guardian=demo_guardian,
            )
        from homework.demo import seed_homework

        seed_homework(school=school, year=year, section=section, subject=subject, teacher_user=demo_teacher)
        from admissions.demo import seed_admissions

        seed_admissions(school=school, level=level, admin=demo_admin)
        if opts["admin_username"]:
            user = User.objects.filter(username=opts["admin_username"]).first()
            if user is None:
                raise CommandError("Create the administrator with createsuperuser first.")
            if user.school_id not in (None, school.pk):
                raise CommandError("That administrator already belongs to another school.")
            user.school = school
            user.save(update_fields=["school"])
            user.groups.add(Group.objects.get(name="Administrator"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo school ready: {school.name}. Eight role accounts; Class 1 with fees, attendance, marks, homework, leave and a download; admissions for 2027; Cambridge IGCSE, IB Diploma and national Class 9 classes with published results; no messages sent."
            )
        )
