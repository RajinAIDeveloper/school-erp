from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    school = models.ForeignKey("core.School", null=True, blank=True, on_delete=models.PROTECT, related_name="users")
    phone = models.CharField(max_length=25, blank=True)
    avatar = models.ImageField(upload_to="users/avatars/", blank=True)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def role_names(self):
        return ", ".join(self.groups.values_list("name", flat=True))
