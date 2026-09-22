from decimal import Decimal

from django.conf import settings
from django.db import models

from academics.models import AcademicYear, ClassLevel, Subject, Term
from core.models import SchoolScopedModel
from students.models import Enrollment


class GradeScale(SchoolScopedModel):
    """Bangladesh GPA scale by default: A+ 80-100 (5.00) ... F 0-32 (0.00)."""

    name = models.CharField(max_length=50, default="Standard")
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_grade_scale_per_school")]

    def __str__(self):
        return self.name

    def grade_for(self, percent):
        for rule in self.rules.all():
            if rule.min_percent <= percent <= rule.max_percent:
                return rule
        return None

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            GradeScale.objects.filter(school=self.school).exclude(pk=self.pk).update(is_default=False)


class GradeRule(models.Model):
    scale = models.ForeignKey(GradeScale, on_delete=models.CASCADE, related_name="rules")
    letter = models.CharField(max_length=5)
    min_percent = models.DecimalField(max_digits=5, decimal_places=2)
    max_percent = models.DecimalField(max_digits=5, decimal_places=2)
    grade_point = models.DecimalField(max_digits=4, decimal_places=2)

    class Meta:
        ordering = ["-min_percent"]

    def __str__(self):
        return f"{self.letter} ({self.min_percent}-{self.max_percent})"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.min_percent is not None and self.max_percent is not None:
            if not 0 <= self.min_percent <= self.max_percent <= 100:
                raise ValidationError("Grade boundaries must be ordered and between 0 and 100.")
        if self.grade_point is not None and self.grade_point < 0:
            raise ValidationError("Grade points must not be negative.")


BD_GRADE_RULES = [
    ("A+", 80, 100, "5.00"),
    ("A", 70, 79.99, "4.00"),
    ("A-", 60, 69.99, "3.50"),
    ("B", 50, 59.99, "3.00"),
    ("C", 40, 49.99, "2.00"),
    ("D", 33, 39.99, "1.00"),
    ("F", 0, 32.99, "0.00"),
]


def ensure_default_grade_scale(school):
    scale, created = GradeScale.objects.get_or_create(school=school, name="Standard", defaults={"is_default": True})
    if created or not scale.rules.exists():
        for letter, lo, hi, gp in BD_GRADE_RULES:
            GradeRule.objects.create(scale=scale, letter=letter, min_percent=lo, max_percent=hi, grade_point=gp)
    return scale


class Exam(SchoolScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ONGOING = "ongoing", "Ongoing"
        PUBLISHED = "published", "Results published"

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="exams")
    term = models.ForeignKey(Term, null=True, blank=True, on_delete=models.SET_NULL, related_name="exams")
    name = models.CharField(max_length=100, help_text="e.g. First Terminal, Half Yearly, Annual")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    grade_scale = models.ForeignKey(GradeScale, on_delete=models.PROTECT, related_name="exams")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    publication_version = models.PositiveIntegerField(default=0)
    grading_snapshot = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-academic_year__start_date", "start_date", "name"]
        constraints = [models.UniqueConstraint(fields=["academic_year", "name"], name="unique_exam_name_per_year")]

    def __str__(self):
        return f"{self.name} {self.academic_year}"


class ExamSchedule(SchoolScopedModel):
    """One subject paper of an exam for one class."""

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="schedules")
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="exam_schedules")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="exam_schedules")
    date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    full_marks = models.DecimalField(max_digits=6, decimal_places=2, default=100)
    pass_marks = models.DecimalField(max_digits=6, decimal_places=2, default=33)
    room = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["date", "start_time", "subject__name"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "class_level", "subject"], name="unique_exam_subject_per_class")
        ]

    def __str__(self):
        return f"{self.exam} - {self.class_level} - {self.subject}"

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.full_marks is not None and self.pass_marks is not None:
            if self.full_marks <= 0 or not 0 <= self.pass_marks <= self.full_marks:
                raise ValidationError("Full marks must be positive and pass marks between zero and full marks.")


class Mark(SchoolScopedModel):
    schedule = models.ForeignKey(ExamSchedule, on_delete=models.CASCADE, related_name="marks")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="marks")
    marks_obtained = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    is_absent = models.BooleanField(default=False)
    remarks = models.CharField(max_length=100, blank=True)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    version = models.PositiveIntegerField(default=1)

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if not self.schedule_id or not self.enrollment_id:
            return
        if (
            self.schedule.class_level_id != self.enrollment.class_level_id
            or self.schedule.exam.academic_year_id != self.enrollment.academic_year_id
        ):
            raise ValidationError("The enrollment must match the exam year and class.")
        if self.is_absent:
            self.marks_obtained = None
        elif (
            self.marks_obtained is None
            or not self.marks_obtained.is_finite()
            or not 0 <= self.marks_obtained <= self.schedule.full_marks
        ):
            raise ValidationError("Enter a score within full marks or mark the student absent.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["schedule", "enrollment"], name="one_mark_per_student_paper")]

    def __str__(self):
        return f"{self.enrollment.student} {self.schedule.subject}: {self.marks_obtained}"

    @property
    def percent(self):
        if self.is_absent or self.marks_obtained is None or not self.schedule.full_marks:
            return Decimal("0")
        return (self.marks_obtained / self.schedule.full_marks) * 100

    @property
    def passed(self):
        return (
            not self.is_absent and self.marks_obtained is not None and self.marks_obtained >= self.schedule.pass_marks
        )


class ResultSnapshot(SchoolScopedModel):
    exam = models.ForeignKey(Exam, on_delete=models.PROTECT, related_name="snapshots")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT, related_name="result_snapshots")
    version = models.PositiveIntegerField()
    payload = models.JSONField()
    import uuid

    verification_code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["exam", "enrollment", "version"], name="unique_result_snapshot_version")
        ]


class UnlockRequest(SchoolScopedModel):
    schedule = models.ForeignKey(ExamSchedule, on_delete=models.PROTECT, related_name="unlock_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="mark_unlock_requests"
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")],
        default="pending",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_unlock_requests",
    )
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


def compute_result(exam, enrollment):
    """Compatibility adapter using the same live or published snapshot as report screens."""
    from .services import build_result_sheet

    sheet = build_result_sheet(exam, enrollment.class_level, enrollment.section)
    row = next((r for r in sheet["rows"] if r["enrollment_id"] == enrollment.pk), None)
    if row is None:
        return None
    return {
        "rows": row["cells"],
        "total": Decimal(row["total"]),
        "full_total": Decimal(row["full_total"]),
        "percent": Decimal(row["percent"]) if row["percent"] is not None else None,
        "gpa": Decimal(row["gpa"]) if row["gpa"] is not None else None,
        "passed": row["result"] == "PASS",
        "result": row["result"],
        "rank": row["rank"],
    }
