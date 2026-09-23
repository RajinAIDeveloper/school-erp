from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from academics.models import AcademicYear, ClassLevel, Subject, Term
from core.models import AssessmentSystem, SchoolScopedModel
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
    assessment_system = models.CharField(
        max_length=10,
        blank=True,
        choices=AssessmentSystem.choices,
        help_text=(
            "Leave blank to follow each class's setting. Set it for an internal test, such as a "
            "class test, that should not use the board's GPA rules."
        ),
    )
    show_rank = models.BooleanField(
        "Show positions",
        null=True,
        blank=True,
        help_text=(
            "Leave blank for the rulebook's usual practice: positions for national-curriculum and "
            "the school's own rules, none for Cambridge, Edexcel and IB."
        ),
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    publication_version = models.PositiveIntegerField(default=0)
    grading_snapshot = models.JSONField(default=list, blank=True)
    # The scale of any paper graded on its own scale, frozen with the exam's at first publication:
    # {schedule id: rules}. A correction published later grades every paper as the first did.
    paper_grading_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-academic_year__start_date", "start_date", "name"]
        constraints = [models.UniqueConstraint(fields=["academic_year", "name"], name="unique_exam_name_per_year")]

    def __str__(self):
        return f"{self.name} {self.academic_year}"

    def rules_for(self, class_level):
        """Which rulebook this exam's results follow in one class."""
        return self.assessment_system or class_level.rules


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
    grade_scale = models.ForeignKey(
        GradeScale,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="papers",
        help_text=(
            "Leave blank to use the exam's scale. Set it for a subject graded on another board's "
            "scale, for example Edexcel 9-1 Mathematics in a Cambridge year."
        ),
    )
    max_grade = models.CharField(
        max_length=5,
        blank=True,
        help_text=(
            "The highest grade this paper can earn, for a tiered entry: C for Cambridge IGCSE Core, "
            "5 for Core on a 9-1 scale. Leave blank for no cap."
        ),
    )

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


class PaperComponent(SchoolScopedModel):
    """
    One part of a paper, with its own pass mark.

    Under the national curriculum a paper is split into creative questions, multiple choice
    and sometimes a practical, and a student must reach 33% in each part as well as overall.
    A paper with no components is marked as a single score, as before.
    """

    schedule = models.ForeignKey(ExamSchedule, on_delete=models.CASCADE, related_name="components")
    code = models.SlugField(max_length=20, help_text="Short key, e.g. cq, mcq, practical.")
    name = models.CharField(max_length=50)
    full_marks = models.DecimalField(max_digits=6, decimal_places=2)
    pass_marks = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Percent of the paper this part is worth, when parts are scaled rather than added: "
            "Cambridge Paper 2 at 40% from 80 raw marks, for example. Leave every part blank to add "
            "the raw marks."
        ),
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["schedule", "code"], name="unique_component_per_paper")]

    def __str__(self):
        return f"{self.schedule} - {self.name}"

    def clean(self):
        super().clean()
        if self.full_marks is not None and self.pass_marks is not None:
            if self.full_marks <= 0 or not 0 <= self.pass_marks <= self.full_marks:
                raise ValidationError("A part needs positive full marks and a pass mark between zero and full marks.")


class Mark(SchoolScopedModel):
    schedule = models.ForeignKey(ExamSchedule, on_delete=models.CASCADE, related_name="marks")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="marks")
    marks_obtained = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Scores per part, keyed by component code, when the paper has parts. The total above is
    # always their sum. Kept on the mark row so the published-marks lock covers them too.
    component_marks = models.JSONField(default=dict, blank=True)
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
            self.component_marks = {}
            return
        components = list(self.schedule.components.all()) if self.schedule.pk else []
        if components:
            self.marks_obtained = self._total_from_components(components)
        if (
            self.marks_obtained is None
            or not self.marks_obtained.is_finite()
            or not 0 <= self.marks_obtained <= self.schedule.full_marks
        ):
            raise ValidationError("Enter a score within full marks or mark the student absent.")

    def _total_from_components(self, components):
        """
        Check every part is present and in range, and return the paper's total.

        Parts with weights are scaled: each part's share of its own raw maximum, times its
        weight, as a share of the paper's full marks. Parts without weights are added.
        """
        from decimal import InvalidOperation

        known = {component.code: component for component in components}
        unknown = set(self.component_marks) - set(known)
        if unknown:
            raise ValidationError(f"Unknown part(s) for this paper: {', '.join(sorted(unknown))}.")
        total = Decimal("0")
        cleaned = {}
        for code, component in known.items():
            raw = self.component_marks.get(code)
            if raw in (None, ""):
                raise ValidationError(f"Enter the {component.name} score, or mark the student absent.")
            try:
                score = Decimal(str(raw))
            except (InvalidOperation, ValueError) as exc:
                raise ValidationError(f"{component.name}: '{raw}' is not a number.") from exc
            if not score.is_finite() or not 0 <= score <= component.full_marks:
                raise ValidationError(f"{component.name} must be between 0 and {component.full_marks}.")
            cleaned[code] = str(score.quantize(Decimal("0.01")))
            total += score
        self.component_marks = cleaned
        weights = [component.weight for component in components]
        if any(weight is not None for weight in weights):
            if any(weight is None for weight in weights) or sum(weights) != 100:
                raise ValidationError("A weighted paper needs a weight on every part, adding up to 100%.")
            share = sum(
                (Decimal(cleaned[c.code]) / c.full_marks * c.weight for c in components),
                Decimal("0"),
            )
            return (share * self.schedule.full_marks / 100).quantize(Decimal("0.01"))
        return total.quantize(Decimal("0.01"))

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


class ResultComment(SchoolScopedModel):
    """
    A teacher's words on one student's result: per subject with an effort grade, or overall
    from the class teacher when no subject is given.

    Written before publication and frozen into the published card with everything else.
    """

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="comments")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="result_comments")
    subject = models.ForeignKey(Subject, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    effort = models.CharField(
        max_length=20,
        blank=True,
        help_text="Effort grade, or approaches to learning in an IB class, as the school reports it.",
    )
    comment = models.TextField(max_length=600, blank=True)
    written_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["exam", "enrollment", "subject"],
                condition=models.Q(subject__isnull=False),
                name="one_subject_comment_per_student",
            ),
            models.UniqueConstraint(
                fields=["exam", "enrollment"],
                condition=models.Q(subject__isnull=True),
                name="one_overall_comment_per_student",
            ),
        ]

    def __str__(self):
        return f"{self.enrollment.student} {self.subject or 'overall'}: {self.comment[:30]}"


class GradeForecast(SchoolScopedModel):
    """
    A grade the school expects, predicts or sets as a target for one student in one subject.

    Each is a dated record, kept alongside earlier ones rather than overwriting them, and shown
    on cards only once approved. It is the school's own judgement, never calculated from
    marks, and never presented as an award by Cambridge, Pearson or the IB.
    """

    class Kind(models.TextChoices):
        PREDICTED = "predicted", "Predicted grade"
        FORECAST = "forecast", "Forecast grade"
        TARGET = "target", "Target grade"

    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="forecasts")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="forecasts")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    grade = models.CharField(max_length=5)
    as_of = models.DateField()
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-as_of", "-id"]
        indexes = [models.Index(fields=["enrollment", "subject", "kind"])]

    def __str__(self):
        return f"{self.enrollment.student} {self.subject} {self.get_kind_display()}: {self.grade}"
