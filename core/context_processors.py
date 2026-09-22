from django.conf import settings

from .roles import ROLE_LABELS, user_roles

def current_school(request):
    user = getattr(request, "user", None)
    school = getattr(user, "school", None) if user and user.is_authenticated else None
    return {"current_school": school}


def erp_context(request):
    school = getattr(request, "school", None)
    user = getattr(request, "user", None)
    roles = user_roles(user) if user is not None and user.is_authenticated else set()
    from academics.models import AcademicYear
    return {
        "current_school": school,
        "current_year": AcademicYear.current_for(school) if school else None,
        "CURRENCY_SYMBOL": school.currency_symbol if school else settings.ERP_DEFAULT_CURRENCY_SYMBOL,
        "user_roles": roles,
        "ROLE_LABELS": ROLE_LABELS,
    }
