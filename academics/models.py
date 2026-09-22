from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import SchoolScopedModel


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


class Section(SchoolScopedModel):
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=20, help_text="e.g. A, B, Morning")
    capacity = models.PositiveSmallIntegerField(default=40)
    class_teacher = models.ForeignKey(
        "employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="class_teacher_of"
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
    code = models.CharField(max_length=20, blank=True)
    class_levels = models.ManyToManyField(ClassLevel, related_name="subjects", blank=True)
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="unique_subject_per_school"),
        ]

    def __str__(self):
        return self.name


class SubjectTeacher(SchoolScopedModel):
    """Which teacher teaches which subject in which section, per academic year."""
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="subject_teachers")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="subject_teachers")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="subject_teachers")
    teacher = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="subject_assignments")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "section", "subject"], name="unique_subject_teacher_per_section"
            ),
        ]

    def __str__(self):
        return f"{self.subject} / {self.section} - {self.teacher}"
