"""
Homework: work a teacher sets for one or more sections, and each student's record of it.

A task holds what to do; each section it is set for has its own due time, because sections
have their lessons on different days. Publishing creates one record per student expected to
do it: only the students who take the subject, so a Humanities student never gets Physics
work. Most work in a national-curriculum school is done in an exercise book and checked in
class, so a record can be kept entirely by the teacher; a student, or a guardian on the
family's phone, can also tick work done at home.

"Missing" is never stored. It is work not done, or not yet checked once its due time has
passed, and it is worked out when asked, so a changed due date needs no clean-up.
"""

import uuid
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import HttpsURLField, SchoolScopedModel


class Task(SchoolScopedModel):
    class Kind(models.TextChoices):
        PRACTICE = "practice", "Practice"
        READING = "reading", "Reading"
        REVISION = "revision", "Revision"
        PROJECT = "project", "Project"
        PREPARATION = "preparation", "Preparation"
        OTHER = "other", "Other"

    class HandIn(models.TextChoices):
        IN_CLASS = "in_class", "Checked in class"
        ONLINE = "online", "Handed in online"
        NONE = "none", "Nothing to hand in"

    class Marking(models.TextChoices):
        NONE = "none", "No mark"
        MARKS = "marks", "Marks"
        GRADE = "grade", "Grade"
        COMMENT = "comment", "Comment only"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        WITHDRAWN = "withdrawn", "Withdrawn"

    academic_year = models.ForeignKey("academics.AcademicYear", on_delete=models.PROTECT, related_name="homework_tasks")
    class_level = models.ForeignKey("academics.ClassLevel", on_delete=models.PROTECT, related_name="homework_tasks")
    # The subject as it is taught: a whole subject, or one paper of it (Bangla 1st paper).
    subject = models.ForeignKey("academics.Subject", on_delete=models.PROTECT, related_name="homework_tasks")
    title = models.CharField(max_length=150)
    purpose = models.CharField(
        max_length=200, blank=True, help_text="What this practises, in a line. Families see it first."
    )
    instructions = models.TextField(blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PRACTICE)
    hand_in = models.CharField(max_length=10, choices=HandIn.choices, default=HandIn.IN_CLASS)
    estimated_minutes = models.PositiveSmallIntegerField(
        default=20, help_text="About how long a typical student needs."
    )
    marking = models.CharField(max_length=10, choices=Marking.choices, default=Marking.NONE)
    max_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    marks_visible = models.BooleanField(
        default=True, help_text="Show the mark to the student and family once the work is returned."
    )
    allow_late = models.BooleanField(default=True, help_text="Work may still be handed in after the due time.")
    selected_only = models.BooleanField(default=False, help_text="Set for chosen students only, not the whole section.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # A published task becomes visible at this moment: now, or later for a scheduled one.
    publish_at = models.DateTimeField(null=True, blank=True)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    teacher = models.ForeignKey(
        "employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="homework_tasks"
    )
    copied_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-publish_at", "-pk"]
        indexes = [
            models.Index(fields=["school", "academic_year", "status"]),
            models.Index(fields=["teacher", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(estimated_minutes__gte=1, estimated_minutes__lte=300),
                name="homework_minutes_in_range",
            ),
            models.CheckConstraint(
                condition=Q(marking="marks", max_marks__gt=0) | (~Q(marking="marks") & Q(max_marks__isnull=True)),
                name="homework_max_marks_only_when_marked",
            ),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(publish_at__isnull=False),
                name="homework_published_has_a_time",
            ),
        ]

    def __str__(self):
        return self.title

    def is_live(self, now=None):
        """Families can see it: published, and its publishing time has come."""
        now = now or timezone.now()
        return self.status == self.Status.PUBLISHED and self.publish_at is not None and self.publish_at <= now

    def is_scheduled(self, now=None):
        now = now or timezone.now()
        return self.status == self.Status.PUBLISHED and self.publish_at is not None and self.publish_at > now


class TaskSection(SchoolScopedModel):
    """One section a task is set for, with that section's due time."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="targets")
    section = models.ForeignKey("academics.Section", on_delete=models.PROTECT, related_name="homework_targets")
    due_at = models.DateTimeField()
    # The school-day date of due_at, so calendars and daily loads are plain date lookups.
    due_on = models.DateField(editable=False)

    class Meta:
        ordering = ["due_at", "section__name"]
        constraints = [models.UniqueConstraint(fields=["task", "section"], name="one_target_per_task_section")]
        indexes = [models.Index(fields=["section", "due_on"])]

    def __str__(self):
        return f"{self.task} · {self.section}"

    def save(self, *args, **kwargs):
        self.due_on = timezone.localdate(self.due_at)
        super().save(*args, **kwargs)


class Submission(SchoolScopedModel):
    """One student's record of one task."""

    class Status(models.TextChoices):
        PENDING = "pending", "Not checked yet"
        DONE = "done", "Done"
        PARTIAL = "partial", "Partly done"
        NOT_DONE = "not_done", "Not done"
        ABSENT = "absent", "Absent"
        EXCUSED = "excused", "Excused"

    class Source(models.TextChoices):
        NONE = "", "—"
        ONLINE = "online", "Handed in online"
        FAMILY = "family", "Ticked done at home"
        TEACHER = "teacher", "Checked by the teacher"

    # Handed in: counted as done in every figure.
    HANDED_IN = (Status.DONE, Status.PARTIAL)
    # Left out of every rate: the student was not expected to do it.
    NOT_EXPECTED = (Status.ABSENT, Status.EXCUSED)

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="submissions")
    target = models.ForeignKey(TaskSection, on_delete=models.CASCADE, related_name="submissions")
    enrollment = models.ForeignKey("students.Enrollment", on_delete=models.CASCADE, related_name="homework")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.NONE, blank=True)
    late = models.BooleanField(default=False)
    attempt = models.PositiveSmallIntegerField(default=1)
    handed_in_at = models.DateTimeField(null=True, blank=True)
    handed_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    by_guardian = models.BooleanField(default=False)
    note = models.CharField(max_length=500, blank=True)
    mark = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    grade = models.CharField(max_length=10, blank=True)
    feedback = models.CharField(max_length=1000, blank=True)
    reason = models.CharField(max_length=150, blank=True, help_text="Why the student was excused.")
    returned_at = models.DateTimeField(null=True, blank=True)
    redo_requested = models.BooleanField(default=False)
    extended_to = models.DateTimeField(null=True, blank=True, help_text="A later due time for this student only.")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["target__section__name", "enrollment__roll_number"]
        constraints = [
            models.UniqueConstraint(fields=["task", "enrollment"], name="one_submission_per_task_student"),
            models.CheckConstraint(condition=Q(mark__isnull=True) | Q(mark__gte=0), name="homework_mark_not_negative"),
        ]
        indexes = [
            models.Index(fields=["enrollment", "status"]),
            models.Index(fields=["target", "status"]),
        ]

    def __str__(self):
        return f"{self.task} · {self.enrollment}"

    @property
    def due_at(self):
        return self.extended_to or self.target.due_at

    @property
    def handed_in(self):
        return self.status in self.HANDED_IN

    def is_missing(self, now=None):
        now = now or timezone.now()
        if self.status == self.Status.NOT_DONE:
            return True
        return self.status == self.Status.PENDING and self.due_at <= now

    @property
    def has_response(self):
        """Anything recorded by anyone: a status, a tick, a mark or a word of feedback."""
        return bool(
            self.status != self.Status.PENDING
            or self.source
            or self.mark is not None
            or self.grade
            or self.feedback
            or self.checked_at
        )


class DailyLimit(SchoolScopedModel):
    """The most homework, in minutes, a class should have due on one day. No row, no warning."""

    class_level = models.ForeignKey("academics.ClassLevel", on_delete=models.CASCADE, related_name="homework_limits")
    minutes = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["class_level__order"]
        constraints = [
            models.UniqueConstraint(fields=["school", "class_level"], name="one_homework_limit_per_class"),
            models.CheckConstraint(condition=Q(minutes__gte=1, minutes__lte=600), name="homework_limit_in_range"),
        ]

    def __str__(self):
        return f"{self.class_level}: {self.minutes} min"


def hand_in_path(instance, filename):
    """Where a hand-in is stored: by school and year, under a random name, never the child's."""
    task = instance.submission.task
    return f"homework/{task.school_id}/{task.academic_year_id}/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


def resource_path(instance, filename):
    return f"homework/{instance.school_id}/resources/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


class SubmissionFile(SchoolScopedModel):
    """One page or file of a student's work handed in online."""

    class Kind(models.TextChoices):
        PDF = "pdf", "PDF"
        JPEG = "jpeg", "Photo"
        DOCX = "docx", "Word document"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=hand_in_path, max_length=200)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    size = models.PositiveIntegerField()
    page = models.PositiveSmallIntegerField(default=1)
    attempt = models.PositiveSmallIntegerField(default=1)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["attempt", "page", "pk"]

    def __str__(self):
        return f"{self.submission} · page {self.page}"


class TaskResource(SchoolScopedModel):
    """A worksheet or a link the teacher gives with the task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="resources")
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to=resource_path, blank=True, max_length=200)
    kind = models.CharField(max_length=8, blank=True, choices=SubmissionFile.Kind.choices)
    size = models.PositiveIntegerField(null=True, blank=True)
    url = HttpsURLField(blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(file="") & ~Q(url="")) | (~Q(file="") & Q(url="")),
                name="homework_resource_is_a_file_or_a_link",
            ),
        ]

    def __str__(self):
        return self.title
