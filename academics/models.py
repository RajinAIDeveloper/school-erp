from django.core.exceptions import ValidationError
from django.db import models

from core.models import AssessmentSystem, SchoolScopedModel

RELIGIONS = [
    ("islam", "Islam"),
    ("hinduism", "Hinduism"),
    ("buddhism", "Buddhism"),
    ("christianity", "Christianity"),
    ("other", "Other"),
]


class Group(models.TextChoices):
    """The streams Classes 9 and 10 divide into under the national curriculum."""

    SCIENCE = "science", "Science"
    BUSINESS = "business", "Business Studies"
    HUMANITIES = "humanities", "Humanities"


class AcademicYear(SchoolScopedModel):
    name = models.CharField(max_length=20, help_text="e.g. 2026")
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-start_date"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="unique_year_per_school"),
        ]

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError("End date must be after start date.")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_current:
            AcademicYear.objects.filter(school=self.school).exclude(pk=self.pk).update(is_current=False)

    def __str__(self):
        return self.name

    @classmethod
    def current_for(cls, school):
        return cls.objects.filter(school=school, is_current=True).first()


class Term(SchoolScopedModel):
    """Optional sub-division of a year, e.g. First Term / Half Yearly / Annual."""

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="terms")
    name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(fields=["academic_year", "name"], name="unique_term_per_year"),
        ]

    def __str__(self):
        return f"{self.name} ({self.academic_year})"


class ClassLevel(SchoolScopedModel):
    """Play, Nursery, KG, Class 1 ... Class 10."""

    name = models.CharField(max_length=50)
    order = models.PositiveSmallIntegerField(help_text="Sort order for promotion, e.g. Play=0, Class 1=3")
    assessment_system = models.CharField(
        max_length=10,
        choices=AssessmentSystem.choices,
        blank=True,
        help_text="Leave blank to follow the school's setting.",
    )

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="unique_class_per_school"),
            models.UniqueConstraint(fields=["school", "order"], name="unique_class_order_per_school"),
        ]

    def __str__(self):
        return self.name

    @property
    def next_level(self):
        return ClassLevel.objects.filter(school=self.school, order__gt=self.order).order_by("order").first()

    @property
    def rules(self):
        """The assessment system this class's results follow."""
        return self.assessment_system or self.school.assessment_system

    @property
    def uses_board_rules(self):
        return self.rules == AssessmentSystem.NATIONAL


class Section(SchoolScopedModel):
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=20, help_text="e.g. A, B, Morning")
    capacity = models.PositiveSmallIntegerField(default=40)
    class_teacher = models.ForeignKey(
        "employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="class_teacher_of"
    )
    shift = models.CharField(
        max_length=10,
        blank=True,
        choices=[("morning", "Morning"), ("day", "Day"), ("evening", "Evening")],
        help_text="For a school that runs more than one shift.",
    )
    version = models.CharField(
        max_length=10,
        blank=True,
        choices=[("bangla", "Bangla medium"), ("english", "English version")],
        help_text="Bangla medium or English version of the national curriculum.",
    )

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["class_level__order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["class_level", "name"], name="unique_section_per_class"),
        ]

    def __str__(self):
        return f"{self.class_level} - {self.name}"


class Subject(SchoolScopedModel):
    name = models.CharField(max_length=100)
    name_bn = models.CharField("Name (Bangla)", max_length=100, blank=True)
    code = models.CharField(
        max_length=20, blank=True, help_text="The board subject code, e.g. 101 for Bangla 1st paper."
    )
    class_levels = models.ManyToManyField(ClassLevel, related_name="subjects", blank=True)
    is_optional = models.BooleanField(default=False)
    combines_into = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="papers",
        help_text=(
            "For a subject examined in two papers: the subject both papers count towards. Bangla 1st "
            "paper and Bangla 2nd paper both count towards Bangla, and are graded together."
        ),
    )
    religion = models.CharField(
        max_length=30,
        choices=RELIGIONS,
        blank=True,
        help_text="For a religion paper: only students of this religion sit it.",
    )
    ib_level = models.CharField(
        "IB level",
        max_length=2,
        blank=True,
        choices=[("HL", "Higher Level"), ("SL", "Standard Level")],
        help_text="For IB Diploma subjects. Record Biology HL and Biology SL as two subjects.",
    )
    ib_core = models.CharField(
        "IB core",
        max_length=3,
        blank=True,
        choices=[("tok", "Theory of Knowledge"), ("ee", "Extended Essay"), ("cas", "CAS")],
        help_text=(
            "For the IB Diploma core. TOK and the Extended Essay are graded A to E; CAS is complete "
            "when its mark reaches the paper's pass mark."
        ),
    )

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="unique_subject_per_school"),
        ]

    def __str__(self):
        return self.name


class ClassSubject(SchoolScopedModel):
    """
    Which subjects a class takes in a year, and for which group.

    This is the curriculum as data. When the board adds a subject (new subjects arrive in
    2027, a new curriculum in 2028), the school edits these rows rather than waiting for a
    software release. A class with no rows takes every paper scheduled for it.
    """

    class Kind(models.TextChoices):
        COMPULSORY = "compulsory", "Compulsory"
        CHOICE = "choice", "Choice (taken as a main or a 4th subject)"

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="class_subjects")
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="class_subjects")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="class_subjects")
    group = models.CharField(max_length=12, choices=Group.choices, blank=True, help_text="Blank means every group.")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.COMPULSORY)

    class Meta:
        ordering = ["class_level__order", "group", "kind", "subject__code", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "class_level", "subject", "group"], name="unique_class_subject_per_group"
            )
        ]

    def __str__(self):
        group = self.get_group_display() if self.group else "all groups"
        return f"{self.class_level} {self.academic_year}: {self.subject} ({self.get_kind_display()}, {group})"


class SubjectTeacher(SchoolScopedModel):
    """Which teacher teaches which subject in which section, per academic year."""

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="subject_teachers")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="subject_teachers")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="subject_teachers")
    teacher = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="subject_assignments")

    class Meta:
        ordering = ["section__class_level__order", "section__name", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "section", "subject"], name="unique_subject_teacher_per_section"
            ),
        ]

    def __str__(self):
        return f"{self.subject} / {self.section} - {self.teacher}"
