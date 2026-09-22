from django.utils.deprecation import MiddlewareMixin

from .models import School


class CurrentSchoolMiddleware(MiddlewareMixin):
    """
    Attaches request.school.

    Today: the authenticated user's school (superusers without a school fall back to the
    first active school). Later (SaaS): resolve from the request host / subdomain instead.
    """

    def process_request(self, request):
        school = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            school = user.school
            if school is None and user.is_superuser:
                school = School.objects.filter(is_active=True).order_by("pk").first()
        request.school = school


class ForcePasswordChangeMiddleware(MiddlewareMixin):
    """
    Hold a user on the password screen until they replace a temporary password.

    An office hands out a password on a slip of paper; until it is changed, anyone who
    saw that slip can sign in as that person. Signing out and the password screens
    themselves stay reachable so the user is not locked in a loop.
    """

    ALLOWED_PREFIXES = ("/password/", "/logout/", "/login/", "/static/", "/media/")

    def process_request(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if not getattr(user, "must_change_password", False):
            return None
        path = request.path
        if any(path.startswith(prefix) for prefix in self.ALLOWED_PREFIXES):
            return None
        from django.contrib import messages
        from django.shortcuts import redirect

        messages.warning(request, "Choose your own password before going any further.")
        return redirect("password_change")
