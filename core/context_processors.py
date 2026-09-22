from django.conf import settings
from django.utils.functional import SimpleLazyObject

from .roles import ROLE_LABELS, user_roles


def erp_context(request):
    school = getattr(request, "school", None)
    user = getattr(request, "user", None)
    roles = user_roles(user) if user is not None and user.is_authenticated else set()
    from academics.models import AcademicYear

    def pending_leave():
        """Counted only if a template actually shows the badge, e.g. not on a PDF or export."""
        if not (school and user is not None and user.is_authenticated):
            return 0
        if not user.has_perm("attendance.change_leaverequest"):
            return 0
        from attendance.models import LeaveRequest

        return LeaveRequest.objects.filter(school=school, status="pending").count()

    return {
        "pending_leave_count": SimpleLazyObject(pending_leave),
        "current_school": school,
        "current_year": AcademicYear.current_for(school) if school else None,
        "CURRENCY_SYMBOL": school.currency_symbol if school else settings.ERP_DEFAULT_CURRENCY_SYMBOL,
        "user_roles": roles,
        "ROLE_LABELS": ROLE_LABELS,
    }
