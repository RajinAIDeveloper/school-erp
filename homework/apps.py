from django.apps import AppConfig


class HomeworkConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homework"
    verbose_name = "Homework"

    def ready(self):
        from . import signals  # noqa: F401 - connects the receivers
