from django.urls import path
from core.crud import crud
from academics.models import AcademicYear, Term, ClassLevel, Section, Subject, SubjectTeacher
from employees.models import Department, Designation
from attendance.models import LeaveType
from . import views
app_name = "settings"
urlpatterns = [
    path("", views.SchoolSettingsView.as_view(), name="school"),
    path("sms/", views.SMSSettingsView.as_view(), name="sms"),
    path("audit/", views.AuditLogListView.as_view(), name="audit"),
]
for model, key, fields, columns in [
    (AcademicYear,"year",["name","start_date","end_date","is_current"],[("Year","name"),("Start","start_date"),("End","end_date"),("Current","is_current","bool")]),
    (Term,"term",["academic_year","name","start_date","end_date"],[("Term","name"),("Year","academic_year"),("Start","start_date"),("End","end_date")]),
    (ClassLevel,"class",["name","order"],[("Class","name"),("Order","order")]),
    (Section,"section",["class_level","name","capacity","class_teacher"],[("Section","__str__"),("Teacher","class_teacher"),("Capacity","capacity")]),
    (Subject,"subject",["name","code","class_levels","is_optional"],[("Subject","name"),("Code","code")]),
    (SubjectTeacher,"subject_teacher",["academic_year","section","subject","teacher"],[("Year","academic_year"),("Section","section"),("Subject","subject"),("Teacher","teacher")]),
    (Department,"department",["name"],[("Department","name")]),
    (Designation,"designation",["name"],[("Designation","name")]),
    (LeaveType,"leave_type",["name","days_per_year"],[("Leave type","name"),("Annual days","days_per_year")]),
]:
    urlpatterns += crud(model,"settings",key,fields,columns,actions=(("School settings","settings:school"),))
