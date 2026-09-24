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
    """
    What a view requires, as the view itself declares it.

    Class views carry `permission_required`; function views decorated with
    `require_permission` carry `erp_permission`. Anything with neither is genuinely open,
    which is a fact worth printing rather than hiding behind a guess.
    """
    view_class = getattr(callback, "view_class", None)
    if view_class is not None:
        declared = getattr(view_class, "permission_required", None)
        if declared:
            return declared
        from django.contrib.auth.mixins import LoginRequiredMixin

        return "(sign-in only)" if issubclass(view_class, LoginRequiredMixin) else "(public)"
    declared = getattr(callback, "erp_permission", None)
    if declared:
        return declared
    if getattr(callback, "erp_public", False):
        return "(public)"
    # login_required and friends wrap with functools.wraps, so a wrapper means a sign-in.
    return "(sign-in only)" if hasattr(callback, "__wrapped__") else "(public)"


def view_narrowing(callback):
    """
    Any further limit the view applies for itself, in the view's own words.

    A permission alone overstates what several of these screens actually hand over: the
    staff file is open to whoever owns it, a leave list shows a teacher only their own
    requests. Those checks live inside the view and no amount of reading the permission
    tables would reveal them, so the view declares them and the matrix repeats them.
    """
    view_class = getattr(callback, "view_class", None)
    if view_class is not None:
        return getattr(view_class, "erp_also", "")
    return getattr(callback, "erp_also", "")


def walk(patterns, prefix="", namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from walk(pattern.url_patterns, prefix + str(pattern.pattern), pattern.namespace or namespace)
        elif isinstance(pattern, URLPattern):
            name = f"{namespace}:{pattern.name}" if namespace else (pattern.name or "")
            yield (
                prefix + str(pattern.pattern),
                name,
                view_permission(pattern.callback),
                view_narrowing(pattern.callback),
            )


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
            (url, name, permission, also)
            for url, name, permission, also in walk(get_resolver().url_patterns)
            if not url.startswith(SKIP_PREFIXES)
        ]

        if options["format"] == "markdown":
            self.stdout.write("| URL | View | Permission | Also enforced | " + " | ".join(roles) + " |")
            self.stdout.write("|---|---|---|---|" + "|".join([":-:"] * len(roles)) + "|")
        for url, name, permission, also in sorted(rows):
            marks = []
            for role in roles:
                if permission == "(public)":
                    marks.append("*")
                elif permission == "(sign-in only)":
                    marks.append("o")
                else:
                    marks.append("Y" if permission in groups[role] else ".")
            if options["format"] == "markdown":
                self.stdout.write(f"| /{url} | {name} | `{permission}` | {also or '-'} | " + " | ".join(marks) + " |")
            else:
                self.stdout.write(f"/{url:55} {permission:40} {also or '-':45} " + " ".join(marks))

        self.stdout.write("")
        # Deliberately ASCII: this output is routinely redirected to a file on
        # Windows, where the console encoding would mangle anything else.
        self.stdout.write(
            "Y = allowed | . = 403 | o = any signed-in user, scoped inside the view | * = no sign-in needed."
        )
        self.stdout.write(
            "A Y is necessary, not always sufficient: where 'Also enforced' names a further limit, "
            "the view applies it to the records themselves."
        )
