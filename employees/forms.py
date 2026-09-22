"""Employee forms. Salary is only editable by people who run payroll."""

from core.forms import SchoolModelForm

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
    class Meta:
        model = Employee
        fields = [*BASE_FIELDS, "basic_salary"]

    def __init__(self, *args, can_see_salary=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_see_salary:
            self.fields.pop("basic_salary", None)


class EmployeeDocumentForm(SchoolModelForm):
    class Meta:
        model = EmployeeDocument
        fields = ["title", "file"]
