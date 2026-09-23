from django.conf import settings
from django.db import models

from academics.models import AcademicYear, ClassLevel, Section
from core.models import SchoolScopedModel


def canonical_phone(phone):
    """
    One spelling per mobile number, so a family is never split across two records.

    Bangladeshi numbers are stored the way schools write them, 01XXXXXXXXX, whatever was
    typed: +8801..., 8801..., or with spaces and dashes. Anything that is not a valid
    Bangladeshi mobile is left exactly as entered rather than mangled, because it may be
    a landline or an overseas number the office still needs.
    """
    from django.core.exceptions import ValidationError as _ValidationError

    from messaging.services import normalize_bd_phone

    raw = (phone or "").strip()
    if not raw:
        return ""
    try:
        # normalize_bd_phone returns 8801XXXXXXXXX; the local form is what follows the 88.
        return normalize_bd_phone(raw)[2:]
    except _ValidationError:
        return raw


class Gender(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "O", "Other"


BLOOD_GROUPS = [(bg, bg) for bg in ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]]

RELIGIONS = [
    ("islam", "Islam"),
    ("hinduism", "Hinduism"),
    ("buddhism", "Buddhism"),
    ("christianity", "Christianity"),
    ("other", "Other"),
]


class Student(SchoolScopedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        GRADUATED = "graduated", "Graduated"
        TRANSFERRED = "transferred", "Transferred"
        WITHDRAWN = "withdrawn", "Withdrawn"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="student_profile"
    )
    student_id = models.CharField(max_length=30, help_text="School-issued ID, e.g. 2026-0001")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    name_bn = models.CharField("Name (Bangla)", max_length=200, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField()
    birth_registration_no = models.CharField(max_length=20, blank=True)
    blood_group = models.CharField(max_length=3, choices=BLOOD_GROUPS, blank=True)
    religion = models.CharField(max_length=30, choices=RELIGIONS, blank=True)
    phone = models.CharField(max_length=25, blank=True)
    email = models.EmailField(blank=True)
    photo = models.ImageField(upload_to="students/photos/", blank=True)
    present_address = models.TextField(blank=True)
    permanent_address = models.TextField(blank=True)
    previous_school = models.CharField(max_length=200, blank=True)
    admission_date = models.DateField()
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["student_id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "student_id"], name="unique_student_id_per_school"),
        ]

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def __str__(self):
        return f"{self.full_name} ({self.student_id})"

    @property
    def current_enrollment(self):
        # A list view prefetches this as `current_enrollments`; without that the property
        # would issue one query per row.
        prefetched = getattr(self, "current_enrollments", None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return (
            self.enrollments.filter(academic_year__is_current=True)
            .select_related("section__class_level", "academic_year")
            .first()
        )

    @property
    def primary_guardian(self):
        prefetched = getattr(self, "ordered_guardian_links", None)
        if prefetched is not None:
            return prefetched[0].guardian if prefetched else None
        link = self.guardian_links.filter(is_primary=True).select_related("guardian").first()
        if link is None:
            link = self.guardian_links.select_related("guardian").first()
        return link.guardian if link else None

    @staticmethod
    def next_student_id(school, year_name):
        prefix = f"{year_name}-"
        last = (
            Student.objects.filter(school=school, student_id__startswith=prefix)
            .order_by("-student_id")
            .values_list("student_id", flat=True)
            .first()
        )
        n = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"{prefix}{n:04d}"


class Guardian(SchoolScopedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="guardian_profile"
    )
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=25, help_text="e.g. 017XXXXXXXX")
    email = models.EmailField(blank=True)
    nid = models.CharField("NID", max_length=20, blank=True)
    occupation = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    sms_opt_in = models.BooleanField(
        "Receive SMS", default=True, help_text="Clear this when a family asks not to be texted."
    )

    class Meta:
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        # Normalised on the way in, so every lookup elsewhere can simply compare strings.
        self.phone = canonical_phone(self.phone)
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"phone"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} ({self.phone})"


class StudentGuardian(models.Model):
    class Relation(models.TextChoices):
        FATHER = "father", "Father"
        MOTHER = "mother", "Mother"
        GRANDPARENT = "grandparent", "Grandparent"
        SIBLING = "sibling", "Sibling"
        UNCLE_AUNT = "uncle_aunt", "Uncle / Aunt"
        OTHER = "other", "Other"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    guardian = models.ForeignKey(Guardian, on_delete=models.CASCADE, related_name="student_links")
    relation = models.CharField(max_length=15, choices=Relation.choices)
    is_primary = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "guardian"], name="unique_student_guardian"),
            # A child has one guardian the school rings first. Without this, "who are this
            # child's siblings" and "who do we text" both have more than one answer.
            models.UniqueConstraint(
                fields=["student"],
                condition=models.Q(is_primary=True),
                name="one_primary_guardian_per_student",
            ),
        ]

    def __str__(self):
        return f"{self.guardian} ({self.get_relation_display()} of {self.student})"

    def save(self, *args, **kwargs):
        # Promoting a guardian stands the previous one down, rather than failing on the
        # constraint and leaving the office to work out why.
        if self.is_primary:
            StudentGuardian.objects.filter(student_id=self.student_id, is_primary=True).exclude(pk=self.pk).update(
                is_primary=False
            )
        super().save(*args, **kwargs)


class Enrollment(SchoolScopedModel):
    class Status(models.TextChoices):
        ENROLLED = "enrolled", "Enrolled"
        PROMOTED = "promoted", "Promoted"
        REPEATED = "repeated", "Repeated"
        LEFT = "left", "Left"

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="enrollments")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="enrollments")
    class_level = models.ForeignKey(ClassLevel, on_delete=models.PROTECT, related_name="enrollments")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="enrollments")
    roll_number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ENROLLED)

    class Meta:
        ordering = ["-academic_year__start_date", "class_level__order", "roll_number"]
        constraints = [
            models.UniqueConstraint(fields=["student", "academic_year"], name="one_enrollment_per_year"),
            models.UniqueConstraint(
                fields=["academic_year", "section", "roll_number"], name="unique_roll_per_section_year"
            ),
        ]

    def __str__(self):
        return f"{self.student} - {self.section} ({self.academic_year})"

    def save(self, *args, **kwargs):
        if self.section_id and not self.class_level_id:
            self.class_level = self.section.class_level
        super().save(*args, **kwargs)

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.section_id and self.class_level_id and self.section.class_level_id != self.class_level_id:
            raise ValidationError({"section": "Section must belong to the selected class."})
        if self.pk and self.result_snapshots.exists():
            old = Enrollment.objects.get(pk=self.pk)
            if any(
                getattr(self, f) != getattr(old, f)
                for f in ("student_id", "academic_year_id", "class_level_id", "section_id", "roll_number")
            ):
                raise ValidationError(
                    "An enrollment with published results is historical. Create a new-year enrollment instead."
                )


class StudentDocument(SchoolScopedModel):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to="students/documents/")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.title
