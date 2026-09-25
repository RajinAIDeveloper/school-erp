"""Employee forms. Salary is only editable by people who run payroll."""

from django import forms
from django.db.models import Q

from core.forms import SchoolModelForm
from core.roles import ACCOUNTANT, MANAGERS, STAFF, TEACHER
from users.models import User

from .models import Employee, EmployeeDocument

BASE_FIELDS = [
    "employee_id",
    "employee_type",
    "first_name",
    "last_name",
    "gender",
    "date_of_birth",
    "phone",
    "email",
    "nid",
    "blood_group",
    "religion",
    "photo",
    "address",
    "department",
    "designation",
    "qualification",
    "joining_date",
    "status",
]


class EmployeeForm(SchoolModelForm):
    create_login = forms.BooleanField(
        required=False,
        initial=True,
        label="Create a login when I save this employee",
        help_text="For a new teacher or staff member. The username and temporary password appear once after saving.",
    )

    class Meta:
        model = Employee
        fields = ["user", *BASE_FIELDS, "basic_salary"]

    def __init__(self, *args, can_see_salary=True, can_create_login=False, **kwargs):
        super().__init__(*args, **kwargs)
        login = self.fields["user"]
        login.label = "Existing login"
        login.help_text = "Choose an account you already made. Its saved name and contact details fill in below."
        if can_create_login and not self.instance.pk:
            login.help_text += " Leave blank to create a new login when you save."
        login.queryset = User.objects.none()
        if self.school is not None:
            login.queryset = (
                User.objects.filter(school=self.school)
                .filter(
                    Q(
                        is_active=True,
                        is_superuser=False,
                        student_profile__isnull=True,
                        guardian_profile__isnull=True,
                        employee_profile__isnull=True,
                    )
                    | Q(pk=self.instance.user_id)
                )
                .distinct()
                .order_by("first_name", "last_name", "username")
                .prefetch_related("groups")
            )
        login.label_from_instance = lambda user: f"{user} ({user.username})"
        # A new staff member with an existing login should need only the details that
        # User Management did not collect, such as gender and joining date.
        self.fields["first_name"].required = False
        self.fields["phone"].required = False
        if self.instance.pk or not can_create_login:
            self.fields.pop("create_login")
        if not can_see_salary:
            self.fields.pop("basic_salary", None)
        self.order_fields(["user", "create_login", *BASE_FIELDS, "basic_salary"])

    def clean(self):
        data = super().clean()
        login = data.get("user")
        employee_type = data.get("employee_type")
        if login:
            for field in ("first_name", "last_name", "email", "phone"):
                if not data.get(field):
                    data[field] = getattr(login, field) or ""
        if login and employee_type and login.pk != self.instance.user_id:
            allowed = set(MANAGERS) | ({TEACHER} if employee_type == Employee.Type.TEACHER else {STAFF, ACCOUNTANT})
            if not login.groups.filter(name__in=allowed).exists():
                role = "Teacher" if employee_type == Employee.Type.TEACHER else "Staff"
                self.add_error("user", f"Choose a login with the {role} role, or a school manager's login.")
        for field in ("first_name", "phone"):
            if not data.get(field):
                self.add_error(field, "Enter this detail here or select an existing login that has it.")
        if login:
            data["create_login"] = False
        return data


class EmployeeDocumentForm(SchoolModelForm):
    class Meta:
        model = EmployeeDocument
        fields = ["title", "file"]
