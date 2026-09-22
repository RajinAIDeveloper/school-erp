"""
Creating logins for people who already have a record.

A school office should not have to invent a username, remember which role goes with a
guardian, and then go back to the student record to link the two. Provisioning does all
three in one step, and shows the temporary password exactly once: it is never stored in
readable form, so if it is missed the account is simply reset.
"""

import secrets

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, assert_school
from core.models import AuditLog
from core.roles import GUARDIAN, STAFF, STUDENT, TEACHER

from .models import User

# Avoids characters people misread when a password is written on a slip of paper.
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def temporary_password(length=10):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def unique_username(base):
    """Slug-ish username derived from the person's own identifier, made unique."""
    cleaned = "".join(ch for ch in str(base).lower() if ch.isalnum() or ch in "-._") or "user"
    candidate = cleaned[:140]
    suffix = 2
    while User.objects.filter(username=candidate).exists():
        candidate = f"{cleaned[:135]}-{suffix}"
        suffix += 1
    return candidate


def profile_details(profile):
    """(role, username base, display name) for a student, guardian or employee."""
    label = profile._meta.label_lower
    if label == "students.student":
        return STUDENT, profile.student_id, profile.full_name
    if label == "students.guardian":
        return GUARDIAN, profile.phone or profile.full_name, profile.full_name
    if label == "employees.employee":
        role = TEACHER if profile.employee_type == "teacher" else STAFF
        return role, profile.employee_id, profile.full_name
    raise ValidationError("That kind of record cannot have a login.")


@transaction.atomic
def provision_login(*, school, user, profile, role=None, send_sms=False):
    """
    Create an account for one student, guardian or employee and link it to their record.

    Returns (account, password). The password is returned once for the office to hand
    over; it is stored only as a hash.
    """
    assert_actor_school(user, school)
    assert_school(school, profile)
    if not user.has_perm("users.add_user"):
        raise PermissionDenied
    if profile.user_id:
        raise ValidationError(f"{profile} already has the login '{profile.user.username}'.")

    default_role, base, display = profile_details(profile)
    role = role or default_role
    password = temporary_password()
    account = User.objects.create_user(
        username=unique_username(base),
        password=password,
        school=school,
        first_name=display.split(" ")[0][:150],
        last_name=" ".join(display.split(" ")[1:])[:150],
        email=getattr(profile, "email", "") or "",
        phone=getattr(profile, "phone", "") or "",
    )
    account.must_change_password = True
    account.save(update_fields=["must_change_password"])
    account.groups.add(Group.objects.get(name=role))

    profile.user = account
    profile.save(update_fields=["user", "updated_at"])

    AuditLog.objects.create(
        school=school,
        user=user,
        action="user.provisioned",
        model=account._meta.label,
        object_id=str(account.pk),
        description=f"{account.username} for {profile} as {role}",
    )
    if send_sms:
        _notify_new_login(school, profile, account, password)
    return account, password


def _notify_new_login(school, profile, account, password):
    """Text the credentials when the school has asked for it, never by default."""
    from messaging.notifications import queue

    phone = getattr(profile, "phone", "") or ""
    if not school.notify_admission_sms or not phone:
        return None
    body = (
        f"{school.short_name or school.name}: your login is {account.username} "
        f"and the temporary password is {password}. Please change it after signing in."
    )
    return queue(
        school,
        key="login",
        phone=phone,
        body=body,
        name=account.get_full_name(),
        dedupe_key=f"login:{account.pk}",
    )


@transaction.atomic
def provision_section(*, school, user, section, include_guardians=True, send_sms=False):
    """
    Give a whole section its logins in one pass.

    Returns the rows created, so the screen can offer them as a CSV the office prints
    once and then destroys. Anyone who already has a login is skipped.
    """
    from students.models import Enrollment

    assert_actor_school(user, school)
    assert_school(school, section)
    if not user.has_perm("users.add_user"):
        raise PermissionDenied

    rows = []
    enrollments = (
        Enrollment.objects.filter(school=school, section=section, academic_year__is_current=True)
        .select_related("student")
        .prefetch_related("student__guardian_links__guardian")
        .order_by("roll_number")
    )
    for enrollment in enrollments:
        student = enrollment.student
        if not student.user_id:
            account, password = provision_login(school=school, user=user, profile=student, send_sms=send_sms)
            rows.append(
                {
                    "person": student.full_name,
                    "role": STUDENT,
                    "identifier": student.student_id,
                    "username": account.username,
                    "password": password,
                }
            )
        if include_guardians:
            guardian = student.primary_guardian
            if guardian is not None and not guardian.user_id:
                account, password = provision_login(school=school, user=user, profile=guardian, send_sms=send_sms)
                rows.append(
                    {
                        "person": guardian.full_name,
                        "role": GUARDIAN,
                        "identifier": f"guardian of {student.full_name}",
                        "username": account.username,
                        "password": password,
                    }
                )
    AuditLog.objects.create(
        school=school,
        user=user,
        action="users.provisioned_section",
        description=f"{section}: {len(rows)} login(s) created",
    )
    return rows


@transaction.atomic
def reset_password(*, school, user, account):
    """Issue a new temporary password and require it to be changed at next sign-in."""
    assert_actor_school(user, school)
    if not user.has_perm("users.change_user"):
        raise PermissionDenied
    if account.school_id != school.pk:
        raise PermissionDenied("That account belongs to another school.")
    if account.is_superuser and not user.is_superuser:
        raise PermissionDenied("Only a platform superuser may reset a superuser's password.")
    password = temporary_password()
    account.set_password(password)
    account.must_change_password = True
    account.save(update_fields=["password", "must_change_password"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="user.password_reset",
        model=account._meta.label,
        object_id=str(account.pk),
        description=f"Temporary password issued for {account.username}",
    )
    return password
