"""Employee forms. Salary is only editable by people who run payroll."""

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from core.files import prepare
from core.forms import SchoolModelForm
from core.roles import ACCOUNTANT, MANAGERS, STAFF, TEACHER
from users.models import User

from .models import Employee, EmployeeDocument

DOCUMENT_UPLOADS = (
    ("cv_document", EmployeeDocument.Category.CV, "CV"),
    ("academic_document", EmployeeDocument.Category.ACADEMIC, "Academic document"),
    ("other_document", EmployeeDocument.Category.OTHER, "Other document"),
)
DOCUMENT_ACCEPT = ".pdf,.jpg,.jpeg,.png,.docx"

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
    cv_document = forms.FileField(
        required=False,
        label="CV (optional)",
        help_text="Upload a CV now, or add it later from the employee's page. PDF, photo or DOCX; up to 10 MB.",
        widget=forms.FileInput(attrs={"accept": DOCUMENT_ACCEPT}),
    )
    academic_document = forms.FileField(
        required=False,
        label="Academic document (optional)",
        help_text="For example, a degree or training certificate. You can add more later.",
        widget=forms.FileInput(attrs={"accept": DOCUMENT_ACCEPT}),
    )
    other_document = forms.FileField(
        required=False,
        label="Other document (optional)",
        help_text="For example, an appointment letter. You can add more later.",
        widget=forms.FileInput(attrs={"accept": DOCUMENT_ACCEPT}),
    )

    class Meta:
        model = Employee
        fields = ["user", *BASE_FIELDS, "basic_salary"]

    def __init__(self, *args, can_see_salary=True, can_create_login=False, can_upload_documents=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].help_text = (
            "Optional. The team this person works in, such as Academics. "
            "Add choices under Teachers & Staff → Departments."
        )
        self.fields["designation"].help_text = (
            "Optional. This person's job title, such as Teacher or Assistant Teacher. "
            "Add choices under Teachers & Staff → Designations. "
            "Assign a class teacher separately under Basic Settings → Sections."
        )
        self.fields["qualification"].help_text = (
            "Optional. Write a short summary, such as B.Ed or M.Sc (Physics). "
            "Use the document fields below for certificates or a CV."
        )
        if not can_upload_documents:
            for field, _, _ in DOCUMENT_UPLOADS:
                self.fields.pop(field)
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
        qualification_position = BASE_FIELDS.index("qualification") + 1
        self.order_fields(
            [
                "user",
                "create_login",
                *BASE_FIELDS[:qualification_position],
                *(field for field, _, _ in DOCUMENT_UPLOADS),
                *BASE_FIELDS[qualification_position:],
                "basic_salary",
            ]
        )

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
        for field, _, _ in DOCUMENT_UPLOADS:
            upload = data.get(field)
            if upload:
                try:
                    data[field], _ = prepare(upload)
                except ValidationError as exc:
                    self.add_error(field, exc)
        return data


class EmployeeDocumentForm(SchoolModelForm):
    class Meta:
        model = EmployeeDocument
        fields = ["category", "title", "file"]
        widgets = {"file": forms.FileInput(attrs={"accept": DOCUMENT_ACCEPT})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].help_text = "Choose CV, Academic qualification, or Other document."
        self.fields["title"].help_text = "For example, B.Ed certificate or appointment letter."
        self.fields["file"].help_text = "PDF, photo or DOCX; up to 10 MB."

    def clean_file(self):
        content, _ = prepare(self.cleaned_data["file"])
        return content
