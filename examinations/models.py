import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from academics.models import AcademicYear, ClassLevel, Section, Subject, Term
from core.models import AssessmentSystem, SchoolScopedModel
from students.models import Enrollment, Student


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
    # Excused from the paper by the school (illness with evidence, a disability arrangement):
    # not a score, not an absence, and left out of the student's result.
    is_exempt = models.BooleanField(default=False)
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
        if self.is_absent and self.is_exempt:
            raise ValidationError("A student is either absent or exempt, not both.")
        if self.is_absent or self.is_exempt:
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


class AwardingBody(models.TextChoices):
    CAMBRIDGE = "cambridge", "Cambridge International Education"
    PEARSON = "pearson", "Pearson Edexcel"
    IB = "ib", "International Baccalaureate"
    BOARD = "board", "Bangladesh education board"
    OTHER = "other", "Other awarding body"


class ExamSeries(SchoolScopedModel):
    """
    One sitting of an awarding body's exams, such as Cambridge June 2027: the frame for the
    school's entries to that body and for the official results it sends back.
    """

    body = models.CharField(max_length=10, choices=AwardingBody.choices)
    name = models.CharField(max_length=60, help_text="As the body names it, e.g. June 2027 or May 2027.")
    centre_number = models.CharField(max_length=20, blank=True, help_text="The school's centre number with this body.")
    entry_deadline = models.DateField(null=True, blank=True)
    results_date = models.DateField(null=True, blank=True, help_text="When the body releases results.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-results_date", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["school", "body", "name"], name="unique_series_per_body")]

    def __str__(self):
        return f"{self.get_body_display()} {self.name}"


class SeriesCandidate(SchoolScopedModel):
    """A student entered for a series, with the candidate number the body knows them by."""

    series = models.ForeignKey(ExamSeries, on_delete=models.CASCADE, related_name="candidates")
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT, related_name="series_candidacies")
    candidate_number = models.CharField(max_length=12)
    uci = models.CharField("Unique candidate identifier", max_length=20, blank=True)
    certificate_name = models.CharField(
        max_length=60,
        blank=True,
        help_text="The name as it should print on the certificate, up to 60 characters. Blank uses the student's name.",
    )

    class Meta:
        ordering = ["candidate_number"]
        constraints = [
            models.UniqueConstraint(fields=["series", "student"], name="one_candidacy_per_series"),
            models.UniqueConstraint(fields=["series", "candidate_number"], name="unique_candidate_number_per_series"),
        ]

    def __str__(self):
        return f"{self.candidate_number} {self.student}"


class SeriesEntry(SchoolScopedModel):
    """One syllabus a candidate is entered for in a series. Withdrawn entries are kept."""

    class Tier(models.TextChoices):
        NONE = "", "No tier"
        CORE = "core", "Core"
        EXTENDED = "extended", "Extended"
        FOUNDATION = "foundation", "Foundation"
        HIGHER = "higher", "Higher"

    class Status(models.TextChoices):
        ENTERED = "entered", "Entered"
        WITHDRAWN = "withdrawn", "Withdrawn"

    candidate = models.ForeignKey(SeriesCandidate, on_delete=models.CASCADE, related_name="entries")
    subject = models.ForeignKey(
        Subject, null=True, blank=True, on_delete=models.SET_NULL, related_name="series_entries"
    )
    qualification = models.CharField(max_length=40, help_text="e.g. IGCSE, International AS, IB Diploma.")
    syllabus_code = models.CharField(max_length=20, help_text="Syllabus, specification or unit code, e.g. 0625.")
    syllabus_title = models.CharField(max_length=100)
    option_code = models.CharField(max_length=10, blank=True, help_text="Component or option code, if any.")
    tier = models.CharField(max_length=10, choices=Tier.choices, blank=True)
    level = models.CharField(
        max_length=2, blank=True, choices=[("", "—"), ("HL", "Higher Level"), ("SL", "Standard Level")]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ENTERED)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["syllabus_code"]
        constraints = [
            models.UniqueConstraint(fields=["candidate", "syllabus_code"], name="one_entry_per_syllabus"),
        ]

    def __str__(self):
        return f"{self.candidate} {self.syllabus_code}"


class OfficialResult(SchoolScopedModel):
    """
    A result as the awarding body issued it, recorded from its statement of results.

    Never edited in place: an amended result is a new record that supersedes the old one,
    with the reason, so the history of what the body said stays visible. Imported results
    are seen only by staff until someone other than the importer has checked them.
    """

    class Kind(models.TextChoices):
        SUBJECT = "subject", "Subject or qualification grade"
        UNIT = "unit", "Unit or component"
        OVERALL = "overall", "Overall award (e.g. IB Diploma)"

    candidate = models.ForeignKey(SeriesCandidate, on_delete=models.PROTECT, related_name="official_results")
    entry = models.ForeignKey(SeriesEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name="results")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SUBJECT)
    syllabus_code = models.CharField(max_length=20)
    syllabus_title = models.CharField(max_length=100, blank=True)
    grade = models.CharField(max_length=10)
    points = models.CharField(max_length=10, blank=True, help_text="Points, UMS or percentage uniform mark, if given.")
    source = models.CharField(max_length=100, help_text="Where it came from, e.g. Cambridge statement of results.")
    received_on = models.DateField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    supersedes = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    amendment_reason = models.CharField(max_length=200, blank=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        ordering = ["syllabus_code", "-created_at"]
        indexes = [models.Index(fields=["candidate", "syllabus_code", "is_current"])]

    def __str__(self):
        return f"{self.candidate} {self.syllabus_code}: {self.grade}"


class CombinedResult(SchoolScopedModel):
    """
    A result built from several published exams with weights, such as an annual result of
    30% half-yearly and 70% final. It is published as its own versioned result, and it
    records which version of each exam it used, so a later correction to an exam shows it is
    out of date rather than changing it silently.
    """

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="combined_results")
    name = models.CharField(max_length=60)
    grade_scale = models.ForeignKey(GradeScale, on_delete=models.PROTECT, related_name="combined_results")
    status = models.CharField(max_length=10, default="draft", choices=[("draft", "Draft"), ("published", "Published")])
    publication_version = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    grading_snapshot = models.JSONField(default=list, blank=True)
    # {exam id: publication version} as used by the latest publication.
    sources_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-academic_year__start_date", "name"]
        constraints = [models.UniqueConstraint(fields=["academic_year", "name"], name="unique_combined_per_year")]

    def __str__(self):
        return f"{self.name} ({self.academic_year})"


class CombinedPart(models.Model):
    combined = models.ForeignKey(CombinedResult, on_delete=models.CASCADE, related_name="parts")
    exam = models.ForeignKey(Exam, on_delete=models.PROTECT, related_name="combined_parts")
    weight = models.DecimalField(max_digits=5, decimal_places=2, help_text="Percent of the combined result.")

    class Meta:
        ordering = ["id"]  # as the school listed them
        constraints = [models.UniqueConstraint(fields=["combined", "exam"], name="one_part_per_exam")]

    def __str__(self):
        return f"{self.exam} at {self.weight}%"


class CombinedSnapshot(SchoolScopedModel):
    """One student's combined result as published, frozen like an exam's result snapshot."""

    combined = models.ForeignKey(CombinedResult, on_delete=models.CASCADE, related_name="snapshots")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="combined_snapshots")
    version = models.PositiveIntegerField()
    payload = models.JSONField()
    verification_code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["combined", "enrollment", "version"], name="one_combined_snapshot_per_version"
            )
        ]


class AccessArrangement(SchoolScopedModel):
    """
    An access arrangement asked of an awarding body for one candidate: extra time, a reader,
    a scribe, a separate room. It carries sensitive information about a child, so only the
    school's managers see it, and the guardian's consent is recorded before it is sent.
    """

    class Kind(models.TextChoices):
        EXTRA_TIME = "extra_time", "Extra time"
        READER = "reader", "Reader or computer reader"
        SCRIBE = "scribe", "Scribe or word processor"
        ROOM = "room", "Separate room"
        BREAKS = "breaks", "Supervised rest breaks"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Being prepared"
        APPLIED = "applied", "Applied for"
        APPROVED = "approved", "Approved by the body"
        REFUSED = "refused", "Refused by the body"

    candidate = models.ForeignKey(SeriesCandidate, on_delete=models.CASCADE, related_name="access_arrangements")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    details = models.CharField(max_length=200, blank=True, help_text="e.g. 25% extra time in written papers.")
    evidence = models.CharField(max_length=200, blank=True, help_text="What supports it; keep the documents offline.")
    consent_on = models.DateField(null=True, blank=True, help_text="When the guardian consented to the application.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["candidate__candidate_number", "kind"]

    def __str__(self):
        return f"{self.candidate} {self.get_kind_display()}"


class SeatPlan(SchoolScopedModel):
    """
    Where each candidate sits for one sitting of an exam: the papers that start at the same
    date and time, whichever classes they belong to.

    Stored rather than worked out on the fly, so the door lists, seat stickers and invigilator
    sheets printed for a room always agree. Once printed it is locked; unlocking allows a new
    allocation.
    """

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="seat_plans")
    date = models.DateField()
    start_time = models.TimeField()
    mix_classes = models.BooleanField(
        default=True, help_text="Seat students of different classes side by side, so neighbours sit different papers."
    )
    seats_per_room = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="At most this many in any room, when exam seating is sparser than lessons."
    )
    locked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["date", "start_time"]
        constraints = [models.UniqueConstraint(fields=["exam", "date", "start_time"], name="one_seat_plan_per_sitting")]

    def __str__(self):
        return f"{self.exam} {self.date:%d %b %Y} {self.start_time:%H:%M}"


class Seat(SchoolScopedModel):
    plan = models.ForeignKey(SeatPlan, on_delete=models.CASCADE, related_name="seats")
    room = models.ForeignKey("timetable.Room", on_delete=models.PROTECT, related_name="exam_seats")
    number = models.PositiveSmallIntegerField()
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="exam_seats")

    class Meta:
        ordering = ["room__name", "number"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "enrollment"], name="one_seat_per_candidate_per_sitting"),
            models.UniqueConstraint(fields=["plan", "room", "number"], name="one_candidate_per_seat"),
        ]

    def __str__(self):
        return f"{self.room} seat {self.number}"


# ------------------------------------------------------------------ published results, for analysis


class ExamResultFact(SchoolScopedModel):
    """
    One student's published result in one exam, laid out for analysis.

    Written from the published snapshot whenever an exam is published or republished, and
    replaced as a whole each time, so every analytic agrees with the cards. Nothing here is
    edited by hand; `manage.py rebuild_result_facts` writes it again from the snapshots.
    """

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="result_facts")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="result_facts")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="result_facts")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="+")
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="+")
    section = models.ForeignKey(Section, null=True, on_delete=models.CASCADE, related_name="+")
    version = models.PositiveIntegerField()
    rulebook = models.CharField(max_length=20)
    group = models.CharField(max_length=40, blank=True)
    total = models.DecimalField(max_digits=9, decimal_places=2, null=True)
    full_total = models.DecimalField(max_digits=9, decimal_places=2, null=True)
    percent = models.DecimalField(max_digits=6, decimal_places=2, null=True)
    gpa = models.DecimalField(max_digits=4, decimal_places=2, null=True)
    points = models.DecimalField(max_digits=6, decimal_places=2, null=True)
    result = models.CharField(max_length=12, blank=True)
    section_rank = models.PositiveIntegerField(null=True)
    class_rank = models.PositiveIntegerField(null=True)
    # Whether the published card shows positions; a family sees a position only where it does.
    show_rank = models.BooleanField(default=True)
    subjects_failed = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam", "enrollment"], name="one_result_fact_per_exam")]
        indexes = [
            models.Index(fields=["school", "academic_year"]),
            models.Index(fields=["exam", "class_level", "section"]),
            models.Index(fields=["student"]),
        ]

    def __str__(self):
        return f"{self.exam} · {self.enrollment_id}"


class SubjectResultFact(SchoolScopedModel):
    """One student's published result in one subject of one exam, as the card grades it."""

    exam_fact = models.ForeignKey(ExamResultFact, on_delete=models.CASCADE, related_name="subjects")
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="+")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="+")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="+")
    section = models.ForeignKey(Section, null=True, on_delete=models.CASCADE, related_name="+")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="+")
    subject_name = models.CharField(max_length=150)
    # The paper, when the subject is examined in one; two papers graded together have none.
    schedule = models.ForeignKey(ExamSchedule, null=True, on_delete=models.SET_NULL, related_name="+")
    papers = models.PositiveSmallIntegerField(default=1)
    score = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    full_marks = models.DecimalField(max_digits=8, decimal_places=2, null=True)
    percent = models.DecimalField(max_digits=6, decimal_places=2, null=True)
    letter = models.CharField(max_length=10, blank=True)
    grade_point = models.DecimalField(max_digits=4, decimal_places=2, null=True)
    passed = models.BooleanField(null=True)
    absent = models.BooleanField(default=False)
    is_fourth = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam_fact", "subject"], name="one_subject_fact_per_result")]
        indexes = [
            models.Index(fields=["exam", "subject", "section"]),
            models.Index(fields=["student", "subject"]),
        ]

    def __str__(self):
        return f"{self.exam_fact} · {self.subject_name}"
