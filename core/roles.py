"""
Role definitions. Roles are Django Groups; the setup_roles management command
creates them and assigns model permissions. Views declare permission_required
so that adding a new role is a data change, not a code change.
"""

ADMIN = "Administrator"
PRINCIPAL = "Principal"
ACCOUNTANT = "Accountant"
TEACHER = "Teacher"
STAFF = "Staff"
STUDENT = "Student"
GUARDIAN = "Guardian"

ALL_ROLES = [ADMIN, PRINCIPAL, ACCOUNTANT, TEACHER, STAFF, STUDENT, GUARDIAN]
ROLE_LABELS = {r: r for r in ALL_ROLES}

# app labels whose full permission set a role receives
FULL_APP_ACCESS = {
    ADMIN: [
        "core",
        "users",
        "academics",
        "students",
        "employees",
        "attendance",
        "fees",
        "finance",
        "examinations",
        "timetable",
        "downloads",
        "messaging",
        "holidays",
        "auth",
    ],
    PRINCIPAL: [
        "academics",
        "students",
        "employees",
        "attendance",
        "examinations",
        "timetable",
        "downloads",
        "messaging",
        "holidays",
    ],
    ACCOUNTANT: ["fees", "finance"],
}

# explicit (app_label, codename) permissions for restricted roles
EXPLICIT_PERMS = {
    PRINCIPAL: [
        ("fees", "view_feeinvoice"),
        ("fees", "view_feepayment"),
        ("finance", "view_account"),
        ("finance", "view_journalentry"),
        ("core", "view_school"),
        ("users", "view_user"),
    ],
    ACCOUNTANT: [
        ("students", "view_student"),
        ("students", "view_enrollment"),
        ("students", "view_guardian"),
        ("academics", "view_classlevel"),
        ("academics", "view_section"),
        ("academics", "view_academicyear"),
        ("employees", "view_employee"),
        ("messaging", "add_smsmessage"),
        ("messaging", "view_smsmessage"),
        ("holidays", "view_holiday"),
        ("downloads", "view_downloaditem"),
    ],
    TEACHER: [
        ("students", "view_student"),
        ("students", "view_enrollment"),
        ("students", "view_guardian"),
        ("academics", "view_classlevel"),
        ("academics", "view_section"),
        ("academics", "view_subject"),
        ("academics", "view_academicyear"),
        ("attendance", "add_studentattendance"),
        ("attendance", "change_studentattendance"),
        ("attendance", "view_studentattendance"),
        ("attendance", "view_staffattendance"),
        ("attendance", "add_leaverequest"),
        ("attendance", "view_leaverequest"),
        ("examinations", "view_exam"),
        ("examinations", "view_examschedule"),
        ("examinations", "add_mark"),
        ("examinations", "change_mark"),
        ("examinations", "view_mark"),
        ("timetable", "view_routineslot"),
        ("timetable", "view_period"),
        ("downloads", "view_downloaditem"),
        ("downloads", "add_downloaditem"),
        ("holidays", "view_holiday"),
        ("employees", "view_employee"),
        ("employees", "view_employeedocument"),
    ],
    STAFF: [
        ("attendance", "view_staffattendance"),
        ("attendance", "add_leaverequest"),
        ("attendance", "view_leaverequest"),
        ("downloads", "view_downloaditem"),
        ("holidays", "view_holiday"),
        ("timetable", "view_routineslot"),
    ],
    STUDENT: [
        ("downloads", "view_downloaditem"),
        ("holidays", "view_holiday"),
        ("timetable", "view_routineslot"),
    ],
    GUARDIAN: [
        ("downloads", "view_downloaditem"),
        ("holidays", "view_holiday"),
        ("timetable", "view_routineslot"),
    ],
}


def user_roles(user):
    if not user or not user.is_authenticated:
        return set()
    roles = set(user.groups.values_list("name", flat=True))
    if user.is_superuser:
        roles.add(ADMIN)
    return roles


def has_role(user, *names):
    return bool(user_roles(user) & set(names))
