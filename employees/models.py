from django.conf import settings
from django.db import models

from core.models import SchoolScopedModel
from students.models import BLOOD_GROUPS, RELIGIONS, Gender


class Department(SchoolScopedModel):
    name = models.CharField(max_length=100)

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_department_per_school")]

    def __str__(self):
        return self.name


class Designation(SchoolScopedModel):
    name = models.CharField(max_length=100, help_text="e.g. Head Teacher, Assistant Teacher, Accountant")

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_designation_per_school")]

    def __str__(self):
        return self.name


class Employee(SchoolScopedModel):
    class Type(models.TextChoices):
        TEACHER = "teacher", "Teacher"
        STAFF = "staff", "Staff"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ON_LEAVE = "on_leave", "On leave"
        RESIGNED = "resigned", "Resigned"
        RETIRED = "retired", "Retired"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="employee_profile"
    )
    employee_id = models.CharField(max_length=30, help_text="e.g. EMP-0001")
    employee_type = models.CharField(max_length=10, choices=Type.choices, default=Type.TEACHER)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=25)
    email = models.EmailField(blank=True)
    nid = models.CharField("NID", max_length=20, blank=True)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUPS, blank=True)
    religion = models.CharField(max_length=30, choices=RELIGIONS, blank=True)
    photo = models.ImageField(upload_to="employees/photos/", blank=True)
    address = models.TextField(blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    designation = models.ForeignKey(
        Designation, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees"
    )
    qualification = models.CharField(max_length=200, blank=True, help_text="e.g. M.Sc (Physics), B.Ed")
    joining_date = models.DateField()
    basic_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["employee_id"]
        constraints = [models.UniqueConstraint(fields=["school", "employee_id"], name="unique_employee_id_per_school")]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return self.full_name

    @staticmethod
    def next_employee_id(school):
        last = (
            Employee.objects.filter(school=school, employee_id__startswith="EMP-")
            .order_by("-employee_id")
            .values_list("employee_id", flat=True)
            .first()
        )
        n = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"EMP-{n:04d}"


class EmployeeDocument(SchoolScopedModel):
    """CVs, certificates and other employee files."""

    class Category(models.TextChoices):
        CV = "cv", "CV"
        ACADEMIC = "academic", "Academic qualification"
        OTHER = "other", "Other document"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=150)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.OTHER)
    file = models.FileField(upload_to="employees/documents/")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
