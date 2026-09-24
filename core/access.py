"""Shared object access rules. Querysets are always limited to the user's school."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from core.roles import has_role


def public(view):
    """
    Mark a view as reachable without signing in, on purpose.

    The access matrix otherwise takes any wrapped view (csrf_exempt, require_POST) as
    needing a sign-in; a view that a payment gateway or a stranger must reach says so here.
    """
    view.erp_public = True
    return view


def require_permission(permission, *, also=""):
    """
    Guard a view with a Django permission.

    `also` records, in a few words, any further narrowing the view does for itself —
    "managers only", "own record only". Holding the permission is then necessary but not
    sufficient, and the generated access matrix can say so instead of overstating what a
    role can reach.
    """

    def decorate(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.school is None or not request.school.is_active:
                raise PermissionDenied("An active school is required.")
            if permission and not request.user.has_perm(permission):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        # Declared rather than inferred, so tooling can report what a view requires
        # without having to guess from decorator closures.
        wrapped.erp_permission = permission or "(sign-in only)"
        wrapped.erp_also = also
        return wrapped

    return decorate


def is_manager(user):
    return has_role(user, "Administrator", "Principal")


def plans_timetable(user):
    """Who the routine's planning tools are for: heads, and whoever edits the timetable."""
    return is_manager(user) or user.has_perm("timetable.change_routineslot")


def sections_for(user, school, academic_year=None):
    """
    The sections this person teaches, in one year.

    Without a year this answers for the current session, which is what a register, a
    dashboard and a class roster mean. Pass a year to authorise something historical —
    a past exam's marks or report cards — and the answer comes from that year's teaching
    assignments rather than from whatever the person happens to teach today.
    """
    from academics.models import AcademicYear, Section

    qs = Section.objects.filter(school=school)
    if is_manager(user):
        return qs
    employee = getattr(user, "employee_profile", None)
    if employee is None:
        return qs.none()
    current = AcademicYear.current_for(school)
    year = academic_year if academic_year is not None else current
    if year is None:
        return qs.none()
    condition = Q(subject_teachers__teacher=employee, subject_teachers__academic_year=year)
    # Class teacher is a standing role with no year of its own, so it only speaks for now.
    if current is not None and year.pk == current.pk:
        condition |= Q(class_teacher=employee)
    return qs.filter(condition).distinct()


def taught_sections(user, school):
    """
    Every section this person has ever been assigned to, across all years.

    Used only to populate a picker for historical records; whatever is chosen is then
    authorised against the year of the exam or report it belongs to.
    """
    from academics.models import Section

    qs = Section.objects.filter(school=school)
    if is_manager(user):
        return qs
    employee = getattr(user, "employee_profile", None)
    if employee is None:
        return qs.none()
    return qs.filter(Q(class_teacher=employee) | Q(subject_teachers__teacher=employee)).distinct()


def may_use_section(user, school, section, academic_year=None):
    """Whether this person may act on a section's records for a given year."""
    if is_manager(user):
        return True
    return sections_for(user, school, academic_year).filter(pk=section.pk).exists()


def students_for(user, school):
    """
    The students this person may see.

    A teacher sees the children currently on the roll of a section they currently teach:
    active students, enrolled this year. A former pupil of a section they took three years
    ago is not their business, and neither is one who has since left.
    """
    from students.models import Enrollment, Student

    qs = Student.objects.filter(school=school)
    if is_manager(user) or has_role(user, "Accountant"):
        return qs
    if has_role(user, "Teacher"):
        from academics.models import AcademicYear

        year = AcademicYear.current_for(school)
        if year is None:
            return qs.none()
        return qs.filter(
            status=Student.Status.ACTIVE,
            enrollments__academic_year=year,
            enrollments__status=Enrollment.Status.ENROLLED,
            enrollments__section__in=sections_for(user, school),
        ).distinct()
    return qs.filter(Q(user=user) | Q(guardian_links__guardian__user=user)).distinct()


def assert_school(school, *objects):
    if any(obj is not None and obj.school_id != school.pk for obj in objects):
        raise PermissionDenied("Record belongs to another school.")


def assert_actor_school(user, school):
    if not user.is_active or (not user.is_superuser and user.school_id != school.pk):
        raise PermissionDenied("Your account cannot act on this school.")
