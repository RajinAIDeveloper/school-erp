from django.urls import path

from academics.models import AcademicYear, ClassLevel, ClassSubject, Section, Subject, SubjectTeacher, Term
from academics.views import subject_teacher_create
from attendance.models import LeaveType
from core.crud import crud
from employees.models import Department, Designation

from . import views

app_name = "settings"
urlpatterns = [
    path("", views.settings_hub, name="hub"),
    path("school/", views.SchoolSettingsView.as_view(), name="school"),
    path("initialise/", views.initialise_defaults, name="initialise"),
    path("sms/", views.SMSSettingsView.as_view(), name="sms"),
    path("payments/", views.PaymentSettingsView.as_view(), name="payments"),
    path("notifications/", views.NotificationSettingsView.as_view(), name="notifications"),
    path("policy/", views.PolicySettingsView.as_view(), name="policy"),
    path("audit/", views.AuditLogListView.as_view(), name="audit"),
    path("year/<int:pk>/switch/", views.switch_year, name="year_switch"),
]
YEAR_ROW_FORMS = (
    (
        "Make current",
        "settings:year_switch",
        "academics.change_academicyear",
        "Make this the current academic year for the whole school?",
    ),
)

for model, key, fields, columns in [
    (
        AcademicYear,
        "year",
        ["name", "start_date", "end_date", "is_current"],
        [("Year", "name"), ("Start", "start_date"), ("End", "end_date"), ("Current", "is_current", "bool")],
    ),
    (
        Term,
        "term",
        ["academic_year", "name", "start_date", "end_date"],
        [("Term", "name"), ("Year", "academic_year"), ("Start", "start_date"), ("End", "end_date")],
    ),
    (
        ClassLevel,
        "class",
        ["name", "order", "assessment_system"],
        [("Class", "name"), ("Order", "order"), ("Rulebook", "get_assessment_system_display")],
    ),
    (
        Section,
        "section",
        ["class_level", "name", "capacity", "class_teacher", "shift", "default_room"],
        [("Section", "__str__"), ("Teacher", "class_teacher"), ("Shift", "get_shift_display"), ("Classroom", "default_room"), ("Capacity", "capacity")],
    ),
    (
        Subject,
        "subject",
        ["name", "name_bn", "code", "class_levels", "is_optional", "combines_into", "religion", "ib_level", "ib_core"],
        [("Subject", "name"), ("Code", "code")],
    ),
    (
        SubjectTeacher,
        "subject_teacher",
        ["academic_year", "section", "subject", "teacher"],
        [("Year", "academic_year"), ("Section", "section"), ("Subject", "subject"), ("Teacher", "teacher")],
    ),
    (
        ClassSubject,
        "class_subject",
        ["academic_year", "class_level", "subject", "group", "kind"],
        [
            ("Class", "class_level"),
            ("Year", "academic_year"),
            ("Subject", "subject"),
            ("Group", "get_group_display"),
            ("Kind", "get_kind_display"),
        ],
    ),
    (Department, "department", ["name"], [("Department", "name")]),
    (Designation, "designation", ["name"], [("Designation", "name")]),
    (LeaveType, "leave_type", ["name", "days_per_year"], [("Leave type", "name"), ("Annual days", "days_per_year")]),
]:
    routes = crud(
        model,
        "settings",
        key,
        fields,
        columns,
        actions=(("School settings", "settings:school", "core.change_school"),),
        row_forms=YEAR_ROW_FORMS if model is AcademicYear else (),
        list_permission="academics.change_classsubject" if model is ClassSubject else None,
        prefill_fields=(
            ("academic_year", "class_level", "subject") if model is ClassSubject else
            ("class_level",) if model is Section else
            ("academic_year",) if model is Term else ()
        ),
        create_success_url_name=(
            "academics:subject_setup" if model is ClassSubject else
            "academics:subject_detail" if model is Subject else None
        ),
        detail_url_name=(
            "academics:subject_setup" if model is ClassSubject else
            "academics:subject_detail" if model is Subject else None
        ),
    )
    if model is SubjectTeacher:
        routes = [route for route in routes if route.name != "subject_teacher_create"]
    urlpatterns += routes

urlpatterns += [path("subject-teacher/new/", subject_teacher_create, name="subject_teacher_create")]
