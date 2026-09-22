from django.conf import settings
from django.db import models

from academics.models import ClassLevel
from core.models import SchoolScopedModel


class DownloadCategory(SchoolScopedModel):
    name = models.CharField(max_length=100, help_text="e.g. Notice, Syllabus, Assignment, Form, Routine")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "download categories"
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_download_category_per_school")]

    def __str__(self):
        return self.name


class DownloadItem(SchoolScopedModel):
    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        STAFF = "staff", "Teachers & staff only"
        STUDENTS = "students", "Students & guardians"
        PUBLIC = "public", "Public (no login required)"

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
        if self.audience == self.Audience.PUBLIC:
            return True
        if not user.is_authenticated or user.school_id != self.school_id and not user.is_superuser:
            return False
        if user.is_superuser or user.groups.filter(name__in=["Administrator", "Principal"]).exists():
            return True
        is_staff_member = hasattr(user, "employee_profile") or user.groups.filter(
            name__in=["Teacher", "Staff", "Accountant"]
        ).exists()
        if self.audience == self.Audience.STAFF:
            return is_staff_member
        if self.audience == self.Audience.STUDENTS:
            if is_staff_member:
                return True
            levels = set(self.class_levels.values_list("id", flat=True))
            if not levels:
                return True
            student = getattr(user, "student_profile", None)
            if student:
                enr = student.current_enrollment
                return bool(enr and enr.class_level_id in levels)
            guardian = getattr(user, "guardian_profile", None)
            if guardian:
                from students.models import Enrollment
                return Enrollment.objects.filter(
                    student__guardian_links__guardian=guardian, academic_year__is_current=True,
                    class_level_id__in=levels,
                ).exists()
            return False
        return True
