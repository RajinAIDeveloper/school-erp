from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connection

_override = ContextVar("published_mark_override", default=False)


def register_sqlite_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_function("erp_mark_override", 0, lambda: int(_override.get()))


@contextmanager
def published_mark_write():
    # Only the service calls this after checking a nonexpired, scoped unlock.
    token = _override.set(True)
    old = ""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('school_erp.mark_override',true)")
            old = cursor.fetchone()[0] or ""
            cursor.execute("SELECT set_config('school_erp.mark_override','on',true)")
    try:
        yield
    finally:
        _override.reset(token)
        if connection.vendor == "postgresql" and not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('school_erp.mark_override',%s,true)", [old])
