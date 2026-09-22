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
