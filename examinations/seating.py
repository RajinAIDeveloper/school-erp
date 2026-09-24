"""
Seat plans: who sits where for each sitting of an exam.

A sitting is every paper that starts at the same date and time. The candidates are the
students who sit at least one of those papers, found the same way as their admit cards, so a
Humanities student is never given a seat for Physics. Rooms are filled in order up to their
capacity. With classes mixed, students of different classes alternate, so neighbours are
usually writing different papers.
"""

from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog
from students.models import Enrollment

from .models import Seat, SeatPlan
from .subjects import papers_for, subject_plan


def sittings(exam):
    """[(date, start_time, [schedules])] for every sitting with a date and start time, in order."""
    grouped = defaultdict(list)
    schedules = (
        exam.schedules.filter(date__isnull=False, start_time__isnull=False)
        .select_related("subject", "class_level")
        .order_by("date", "start_time", "class_level__order", "subject__name")
    )
    for schedule in schedules:
        grouped[(schedule.date, schedule.start_time)].append(schedule)
    return [(day, start, papers) for (day, start), papers in grouped.items()]


def candidates(exam, schedules):
    """
    [(enrollment, [papers])] for the students who sit at least one of `schedules`, class by
    class, then by section and roll.
    """
    by_level = defaultdict(list)
    for schedule in schedules:
        by_level[schedule.class_level].append(schedule)
    found = []
    for level in sorted(by_level, key=lambda lv: (lv.order, lv.name)):
        plan = subject_plan(exam.academic_year, level)
        enrollments = (
            Enrollment.objects.filter(
                school=exam.school, academic_year=exam.academic_year, class_level=level, student__status="active"
            )
            .select_related("student", "section", "class_level")
            .prefetch_related("chosen_subjects")
            .order_by("section__name", "roll_number")
        )
        for enrollment in enrollments:
            papers = [schedule for schedule, _role in papers_for(enrollment, by_level[level], plan)]
            if papers:
                found.append((enrollment, papers))
    return found


def seating_order(found, mix):
    """The order seats are filled in: as given, or with the classes taking turns."""
    if not mix:
        return list(found)
    queues = defaultdict(list)
    for item in found:
        queues[item[0].class_level_id].append(item)
    lines, ordered = list(queues.values()), []
    while any(lines):
        for line in lines:
            if line:
                ordered.append(line.pop(0))
    return ordered


def _room_sizes(rooms, seats_per_room):
    return [(room, min(room.capacity, seats_per_room) if seats_per_room else room.capacity) for room in rooms]


@transaction.atomic
def build_plan(*, user, exam, date, start_time, rooms, mix_classes=True, seats_per_room=None):
    """
    Allocate every candidate of one sitting to a room and seat, replacing any earlier
    allocation that has not been printed and locked.
    """
    assert_actor_school(user, exam.school)
    if not user.has_perm("examinations.change_exam"):
        raise PermissionDenied
    for room in rooms:
        assert_school(exam.school, room)
    papers = [papers for day, start, papers in sittings(exam) if (day, start) == (date, start_time)]
    if not papers:
        raise ValidationError("No paper of this exam starts at that date and time.")
    if not rooms:
        raise ValidationError("Choose at least one room.")
    plan, _created = SeatPlan.objects.select_for_update().get_or_create(
        school=exam.school, exam=exam, date=date, start_time=start_time, defaults={"created_by": user}
    )
    if plan.locked_at:
        raise ValidationError("This seat plan has been printed and locked. Unlock it before seating again.")
    order = seating_order(candidates(exam, papers[0]), mix_classes)
    sizes = _room_sizes(rooms, seats_per_room)
    seats = sum(size for _room, size in sizes)
    if len(order) > seats:
        raise ValidationError(
            f"{len(order)} students sit at this time, but the rooms chosen have {seats} seats. "
            f"Add a room or raise the seats per room."
        )
    plan.seats.all().delete()
    plan.mix_classes, plan.seats_per_room = mix_classes, seats_per_room
    plan.save(update_fields=["mix_classes", "seats_per_room", "updated_at"])
    queue = iter(order)
    created = []
    for room, size in sizes:
        for number in range(1, size + 1):
            item = next(queue, None)
            if item is None:
                break
            created.append(Seat(school=exam.school, plan=plan, room=room, number=number, enrollment=item[0]))
    Seat.objects.bulk_create(created)
    AuditLog.objects.create(
        school=exam.school,
        user=user,
        action="exam.seat_plan_built",
        model=SeatPlan._meta.label,
        object_id=str(plan.pk),
        description=f"{plan}: {len(created)} seats in {len({s.room_id for s in created})} room(s)",
    )
    return plan


@transaction.atomic
def set_locked(*, user, plan, locked):
    """Lock a plan once it is printed, or unlock it to seat again."""
    assert_actor_school(user, plan.school)
    if not user.has_perm("examinations.change_exam"):
        raise PermissionDenied
    plan = SeatPlan.objects.select_for_update().get(pk=plan.pk)
    if locked and not plan.seats.exists():
        raise ValidationError("Seat the students before locking the plan.")
    plan.locked_at = timezone.now() if locked else None
    plan.save(update_fields=["locked_at", "updated_at"])
    AuditLog.objects.create(
        school=plan.school,
        user=user,
        action="exam.seat_plan_locked" if locked else "exam.seat_plan_unlocked",
        model=SeatPlan._meta.label,
        object_id=str(plan.pk),
        description=str(plan),
    )
    return plan


def room_lists(plan):
    """[(room, [(seat, papers)])] for printing, with each candidate's papers at this sitting."""
    papers = next((p for day, start, p in sittings(plan.exam) if (day, start) == (plan.date, plan.start_time)), [])
    sitting = {enrollment.pk: taken for enrollment, taken in candidates(plan.exam, papers)} if papers else {}
    rooms = defaultdict(list)
    seats = plan.seats.select_related("room", "enrollment__student", "enrollment__section__class_level")
    for seat in seats.order_by("room__name", "number"):
        rooms[seat.room].append((seat, sitting.get(seat.enrollment_id, [])))
    return list(rooms.items())
