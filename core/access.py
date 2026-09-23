"""Shared object access rules. Querysets are always limited to the user's school."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from core.roles import has_role


def require_permission(permission):
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
        return wrapped

    return decorate


def is_manager(user):
    return has_role(user, "Administrator", "Principal")


def sections_for(user, school):
    from academics.models import Section

    qs = Section.objects.filter(school=school)
    if is_manager(user):
        return qs
    employee = getattr(user, "employee_profile", None)
    if employee:
        return qs.filter(Q(class_teacher=employee) | Q(subject_teachers__teacher=employee)).distinct()
    return qs.none()


def students_for(user, school):
    from students.models import Student

    qs = Student.objects.filter(school=school)
    if is_manager(user) or has_role(user, "Accountant"):
        return qs
    if has_role(user, "Teacher"):
        return qs.filter(enrollments__section__in=sections_for(user, school)).distinct()
    return qs.filter(Q(user=user) | Q(guardian_links__guardian__user=user)).distinct()


def assert_school(school, *objects):
    if any(obj is not None and obj.school_id != school.pk for obj in objects):
        raise PermissionDenied("Record belongs to another school.")


def assert_actor_school(user, school):
    if not user.is_active or (not user.is_superuser and user.school_id != school.pk):
        raise PermissionDenied("Your account cannot act on this school.")
