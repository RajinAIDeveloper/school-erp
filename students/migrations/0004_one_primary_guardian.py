"""
One canonical phone per guardian, and one primary guardian per child.

Both rules are enforced from here on, so existing rows are brought into line first: a
constraint added over data that breaks it would simply fail to apply on a real school's
database.
"""

from django.db import migrations, models


def canonicalise(apps, schema_editor):
    """
    Rewrite guardian phone numbers into the single form 01XXXXXXXXX.

    Nothing is merged or deleted. Where canonicalising makes two guardian records of one
    school share a number, that is reported rather than resolved: only the office knows
    whether those are one family written twice or two people sharing a handset.
    """
    from students.models import canonical_phone

    Guardian = apps.get_model("students", "Guardian")
    changed = 0
    for guardian in Guardian.objects.exclude(phone="").iterator():
        canonical = canonical_phone(guardian.phone)
        if canonical != guardian.phone:
            guardian.phone = canonical
            guardian.save(update_fields=["phone"])
            changed += 1

    seen, collisions = {}, []
    for school_id, pk, phone, name in Guardian.objects.exclude(phone="").values_list(
        "school_id", "pk", "phone", "full_name"
    ):
        key = (school_id, phone)
        if key in seen:
            collisions.append(f"{phone}: {seen[key]} and {name}")
        else:
            seen[key] = name
    if changed or collisions:
        print(f"\n  Guardian phone numbers rewritten to a single form: {changed}.")
    for line in collisions:
        print(f"  Two guardian records now share a number, which the office should check: {line}")


def stand_down_extra_primaries(apps, schema_editor):
    """Keep the earliest primary guardian of each child and stand the rest down."""
    StudentGuardian = apps.get_model("students", "StudentGuardian")
    kept, demoted = set(), []
    for pk, student_id in StudentGuardian.objects.filter(is_primary=True).order_by("pk").values_list(
        "pk", "student_id"
    ):
        if student_id in kept:
            demoted.append(pk)
        else:
            kept.add(student_id)
    if demoted:
        StudentGuardian.objects.filter(pk__in=demoted).update(is_primary=False)
        print(f"\n  Children with more than one primary guardian: {len(demoted)} extra link(s) stood down.")


def noop(apps, schema_editor):
    """Both fixes are one-way tidying; there is nothing to restore going backwards."""


class Migration(migrations.Migration):
    dependencies = [
        ("students", "0003_guardian_sms_opt_in"),
    ]

    operations = [
        migrations.RunPython(canonicalise, noop),
        migrations.RunPython(stand_down_extra_primaries, noop),
        migrations.AddConstraint(
            model_name="studentguardian",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_primary", True)),
                fields=("student",),
                name="one_primary_guardian_per_student",
            ),
        ),
    ]
