from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    school = models.ForeignKey("core.School", null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    phone = models.CharField(max_length=25, blank=True)
    avatar = models.ImageField(upload_to="users/avatars/", blank=True)
    language = models.CharField(
        max_length=5,
        blank=True,
        choices=[("en", "English"), ("bn", "বাংলা")],
        help_text="Blank follows the school's default language.",
    )
    must_change_password = models.BooleanField(
        default=False,
        help_text="Set when an office issues a temporary password. Cleared once the person picks their own.",
    )

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def role_names(self):
        return ", ".join(self.groups.values_list("name", flat=True))

    @property
    def linked_profile_object(self):
        """The student, guardian or employee this account belongs to, if any."""
        for attribute in ("student_profile", "guardian_profile", "employee_profile"):
            profile = getattr(self, attribute, None)
            if profile is not None:
                return profile
        return None

    @property
    def linked_profile(self):
        profile = self.linked_profile_object
        if profile is None:
            return ""
        return f"{profile._meta.verbose_name.title()}: {profile}"
