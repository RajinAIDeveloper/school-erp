from django.conf import settings
from django.db import models
from django.utils import timezone

from academics.models import ClassLevel
from core.models import SchoolScopedModel


class Audience(models.TextChoices):
    """Who a notice or a file is for. Shared so both read the same way."""

    EVERYONE = "everyone", "Everyone at the school"
    STAFF = "staff", "Teachers and staff only"
    STUDENTS = "students", "Students and guardians"
    PUBLIC = "public", "Public, no sign-in needed"


def audience_allows(record, user):
    """
    Whether this person may see a record with this audience.

    Used by both files and notices. A class-targeted record reaches a student in that
    class and the guardians of such a student; staff see everything aimed at staff.
    """
    if record.audience == Audience.PUBLIC:
        return True
    if not user.is_authenticated:
        return False
    if user.school_id != record.school_id and not user.is_superuser:
        return False
    from core.roles import MANAGERS

    if user.is_superuser or user.groups.filter(name__in=MANAGERS).exists():
        return True

    is_staff_member = (
        hasattr(user, "employee_profile") or user.groups.filter(name__in=["Teacher", "Staff", "Accountant"]).exists()
    )
    if record.audience == Audience.STAFF:
        return is_staff_member
    if record.audience == Audience.STUDENTS:
        if is_staff_member:
            return True
        levels = set(record.class_levels.values_list("id", flat=True))
        if not levels:
            return True
        student = getattr(user, "student_profile", None)
        if student:
            enrollment = student.current_enrollment
            return bool(enrollment and enrollment.class_level_id in levels)
        guardian = getattr(user, "guardian_profile", None)
        if guardian:
            from students.models import Enrollment

            return Enrollment.objects.filter(
                student__guardian_links__guardian=guardian,
                academic_year__is_current=True,
                class_level_id__in=levels,
            ).exists()
        return False
    return True


class DownloadCategory(SchoolScopedModel):
    name = models.CharField(max_length=100, help_text="e.g. Notice, Syllabus, Assignment, Form, Routine")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "download categories"
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_download_category_per_school")]

    def __str__(self):
        return self.name


class DownloadItem(SchoolScopedModel):
    category = models.ForeignKey(DownloadCategory, on_delete=models.PROTECT, related_name="items")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to="downloads/%Y/%m/")
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.EVERYONE)
    class_levels = models.ManyToManyField(
        ClassLevel, blank=True, related_name="downloads", help_text="Leave empty for all classes"
    )
    is_active = models.BooleanField(default=True)
    download_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1]

    def visible_to(self, user):
        """Permission check used by the protected download view."""
        return audience_allows(self, user)


class Notice(SchoolScopedModel):
    """
    A short announcement, as opposed to a file to download.

    Notices carry their own audience and dates because the thing a school most often
    needs to publish is a sentence, not an attachment, and it usually stops being true
    on a particular day.
    """

    title = models.CharField(max_length=200)
    body = models.TextField(help_text="Plain text. It is shown as written, never as HTML.")
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.EVERYONE)
    class_levels = models.ManyToManyField(
        ClassLevel, blank=True, related_name="notices", help_text="Leave empty for all classes."
    )
    publish_at = models.DateField(default=timezone.localdate)
    expires_at = models.DateField(null=True, blank=True, help_text="After this date it drops off the boards.")
    is_pinned = models.BooleanField(default=False, help_text="Keep at the top while it matters.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-is_pinned", "-publish_at", "-id"]

    def __str__(self):
        return self.title

    @property
    def is_live(self):
        today = timezone.localdate()
        return self.publish_at <= today and (self.expires_at is None or self.expires_at >= today)

    def visible_to(self, user):
        """Same audience rule as a file, so one mental model covers both."""
        return audience_allows(self, user)


def live_notices_for(user, school):
    """Notices a person should actually see today, pinned ones first."""
    today = timezone.localdate()
    candidates = (
        Notice.objects.filter(school=school, publish_at__lte=today)
        .filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gte=today))
        .prefetch_related("class_levels")
    )
    return [notice for notice in candidates if notice.visible_to(user)]
