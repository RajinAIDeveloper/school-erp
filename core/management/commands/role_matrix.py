"""
Print who can open what.

The access matrix in the documentation goes stale the moment a permission changes, so it
is generated from the URLs and the groups rather than maintained by hand.
"""

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver, get_resolver

from core.roles import ALL_ROLES

SKIP_PREFIXES = ("admin/", "password/", "login/", "logout/", "static/", "media/")


def view_permission(callback):
    """The permission a view declares, whether it is a class or a decorated function."""
    view_class = getattr(callback, "view_class", None)
    if view_class is not None:
        return getattr(view_class, "permission_required", None) or "(sign-in only)"
    function = callback
    for _ in range(5):
        closure = getattr(function, "__closure__", None) or ()
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, str) and "." in value and "_" in value:
                return value
        function = getattr(function, "__wrapped__", None)
        if function is None:
            break
    return "(sign-in only)"


def walk(patterns, prefix="", namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from walk(pattern.url_patterns, prefix + str(pattern.pattern), pattern.namespace or namespace)
        elif isinstance(pattern, URLPattern):
            name = f"{namespace}:{pattern.name}" if namespace else (pattern.name or "")
            yield prefix + str(pattern.pattern), name, view_permission(pattern.callback)


class Command(BaseCommand):
    help = "Print the role access matrix as Markdown, generated from the URLs and groups."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=["markdown", "plain"], default="markdown")

    def handle(self, *args, **options):
        groups = {
            group.name: {
                f"{permission.content_type.app_label}.{permission.codename}"
                for permission in group.permissions.select_related("content_type")
            }
            for group in Group.objects.all()
        }
        roles = [role for role in ALL_ROLES if role in groups]
        rows = [
            (url, name, permission)
            for url, name, permission in walk(get_resolver().url_patterns)
            if not url.startswith(SKIP_PREFIXES)
        ]

        if options["format"] == "markdown":
            self.stdout.write("| URL | View | Permission | " + " | ".join(roles) + " |")
            self.stdout.write("|---|---|---|" + "|".join([":-:"] * len(roles)) + "|")
        for url, name, permission in sorted(rows):
            marks = []
            for role in roles:
                if permission == "(sign-in only)":
                    marks.append("o")
                else:
                    marks.append("Y" if permission in groups[role] else ".")
            if options["format"] == "markdown":
                self.stdout.write(f"| /{url} | {name} | `{permission}` | " + " | ".join(marks) + " |")
            else:
                self.stdout.write(f"/{url:55} {permission:40} " + " ".join(marks))

        self.stdout.write("")
        self.stdout.write("Y = allowed · . = 403 · o = any signed-in user, scoped inside the view.")
