from datetime import date,time
from decimal import Decimal
from django.core.management.base import BaseCommand,CommandError
from django.core.management import call_command
from django.contrib.auth.models import Group
from django.db import transaction
from core.models import School
from academics.models import AcademicYear,ClassLevel,Section,Subject,SubjectTeacher
from students.models import Student,Enrollment,Guardian,StudentGuardian
from employees.models import Employee,Department,Designation
from examinations.models import Exam,ExamSchedule,ensure_default_grade_scale
from fees.models import FeeCategory,FeeStructure
from finance.models import ensure_default_accounts
from holidays.models import Holiday
from timetable.models import Period,Room,RoutineSlot
from downloads.models import DownloadCategory
from messaging.models import SMSTemplate
from users.models import User

class Command(BaseCommand):
    help="Create idempotent demonstration school data. No SMS or email is sent and existing users/passwords are never changed."
    def add_arguments(self,parser):
        parser.add_argument("--school-name",default="School ERP Demo")
        parser.add_argument("--slug",default="demo-school")
        parser.add_argument("--admin-username",default=None)
    @transaction.atomic
    def handle(self,*args,**opts):
        call_command("setup_roles")
        school,_=School.objects.get_or_create(slug=opts["slug"],defaults={"name":opts["school_name"],"short_name":"Demo School","phone":"01700000000"})
        year,_=AcademicYear.objects.get_or_create(school=school,name="2026",defaults={"start_date":date(2026,1,1),"end_date":date(2026,12,31),"is_current":True})
        department,_=Department.objects.get_or_create(school=school,name="Academics")
        designation,_=Designation.objects.get_or_create(school=school,name="Assistant Teacher")
        teacher,_=Employee.objects.get_or_create(school=school,employee_id="DEMO-T1",defaults={"first_name":"Demo","last_name":"Teacher","gender":"F","phone":"01700000000","joining_date":date(2026,1,1),"department":department,"designation":designation,"basic_salary":25000})
        level,_=ClassLevel.objects.get_or_create(school=school,name="Class 1",defaults={"order":1})
        section,_=Section.objects.get_or_create(school=school,class_level=level,name="A",defaults={"class_teacher":teacher})
        subject,_=Subject.objects.get_or_create(school=school,name="Mathematics",defaults={"code":"MATH"})
        subject.class_levels.add(level)
        SubjectTeacher.objects.get_or_create(school=school,academic_year=year,section=section,subject=subject,defaults={"teacher":teacher})
        for n in range(1,6):
            student,_=Student.objects.get_or_create(school=school,student_id=f"DEMO-{n:03}",defaults={"first_name":f"Demo Student {n}","gender":"F" if n%2 else "M","date_of_birth":date(2018,1,n),"admission_date":date(2026,1,1)})
            Enrollment.objects.get_or_create(school=school,student=student,academic_year=year,defaults={"section":section,"class_level":level,"roll_number":n})
            guardian,_=Guardian.objects.get_or_create(school=school,full_name=f"Demo Guardian {n}",defaults={"phone":"01700000000"})
            StudentGuardian.objects.get_or_create(student=student,guardian=guardian,defaults={"relation":"mother","is_primary":True})
        ensure_default_accounts(school)
        category,_=FeeCategory.objects.get_or_create(school=school,name="Tuition")
        FeeStructure.objects.get_or_create(school=school,academic_year=year,class_level=level,category=category,defaults={"amount":1000})
        scale=ensure_default_grade_scale(school)
        exam,_=Exam.objects.get_or_create(school=school,academic_year=year,name="First Term",defaults={"grade_scale":scale,"start_date":date(2026,10,1),"end_date":date(2026,10,5)})
        ExamSchedule.objects.get_or_create(school=school,exam=exam,class_level=level,subject=subject,defaults={"date":date(2026,10,1),"full_marks":100,"pass_marks":33})
        period,_=Period.objects.get_or_create(school=school,order=1,defaults={"name":"Period 1","start_time":time(9),"end_time":time(9,45)})
        room,_=Room.objects.get_or_create(school=school,name="Room 101")
        RoutineSlot.objects.get_or_create(school=school,academic_year=year,section=section,weekday=1,period=period,defaults={"subject":subject,"teacher":teacher,"room":room})
        Holiday.objects.get_or_create(school=school,name="Demo winter holiday",defaults={"start_date":date(2026,12,20),"end_date":date(2026,12,25)})
        DownloadCategory.objects.get_or_create(school=school,name="Learning materials")
        SMSTemplate.objects.get_or_create(school=school,name="Fee reminder",defaults={"body":"Dear guardian, {student} has outstanding fees of {amount}. Please contact {school}."})
        if opts["admin_username"]:
            user=User.objects.filter(username=opts["admin_username"]).first()
            if user is None:
                raise CommandError("Create the administrator with createsuperuser first.")
            if user.school_id not in (None,school.pk):
                raise CommandError("That administrator already belongs to another school.")
            user.school=school
            user.save(update_fields=["school"])
            user.groups.add(Group.objects.get(name="Administrator"))
        self.stdout.write(self.style.SUCCESS(f"Demo school ready: {school.name}. Five students; no messages sent."))
