from django.apps import AppConfig
class ExaminationsConfig(AppConfig):
    default_auto_field="django.db.models.BigAutoField"
    name="examinations"
    def ready(self):
        from django.db.backends.signals import connection_created
        from .locking import register_sqlite_functions
        connection_created.connect(register_sqlite_functions,dispatch_uid="erp.sqlite.mark-lock")
