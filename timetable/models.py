from django.core.exceptions import ValidationError
from django.db import models

from academics.models import AcademicYear, Section, Subject
from core.models import SchoolScopedModel
from employees.models import Employee

WEEKDAYS = [
    (6, "Saturday"),
    (7, "Sunday"),
    (1, "Monday"),
    (2, "Tuesday"),
    (3, "Wednesday"),
    (4, "Thursday"),
    (5, "Friday"),
]


class Period(SchoolScopedModel):
    """A time slot of the school day, e.g. 1st period 09:00-09:45, Tiffin 12:00-12:30."""

    SHIFT_CHOICES = [("morning", "Morning"), ("day", "Day"), ("evening", "Evening")]
    shift = models.CharField(
        max_length=10, blank=True, choices=SHIFT_CHOICES,
        help_text="Leave blank for the default schedule. Choose a shift for its own periods and breaks.",
    )
    name = models.CharField(max_length=50)
    order = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_break = models.BooleanField(default=False)

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(fields=["school", "shift", "order"], name="unique_period_order_per_shift")]

    def __str__(self):
        shift = f"{self.get_shift_display()} · " if self.shift else ""
        return f"{shift}{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})"


class PeriodDayTime(SchoolScopedModel):
    """
    A period at other times on one day of the week, or not held that day: a shorter Thursday,
    a half day, a late start after assembly. Days without one keep the period's own times.
    """

    period = models.ForeignKey(Period, on_delete=models.CASCADE, related_name="day_times")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    not_held = models.BooleanField(default=False)

    class Meta:
        ordering = ["weekday", "period__order"]
        constraints = [models.UniqueConstraint(fields=["period", "weekday"], name="one_timing_per_period_day")]

    def __str__(self):
        if self.not_held:
            return f"{self.period.name}: not held on {self.get_weekday_display()}"
        return f"{self.period.name} on {self.get_weekday_display()}: {self.start_time:%H:%M}-{self.end_time:%H:%M}"


def school_weekdays(school):
    """The days the school meets, in the order of a Bangladeshi week: Saturday first."""
    weekend = school.weekend_day_numbers
    return [(number, label) for number, label in WEEKDAYS if number not in weekend]


def day_times(school):
    """Every different timing the school has set: {(period id, weekday): PeriodDayTime}."""
    return {(row.period_id, row.weekday): row for row in PeriodDayTime.objects.filter(school=school)}


def times_on(period, weekday, timings):
    """(start, end) of a period on a day, or None when it is not held that day."""
    row = timings.get((period.pk, weekday))
    if row is None:
        return period.start_time, period.end_time
    if row.not_held:
        return None
    return row.start_time, row.end_time


class Room(SchoolScopedModel):
    name = models.CharField(max_length=50)
    capacity = models.PositiveSmallIntegerField(default=40)

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_room_per_school")]

    def __str__(self):
        return self.name


class SectionRoomChange(SchoolScopedModel):
    """Use another classroom for a section on particular dates, without changing its routine."""

    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="room_changes")
    start_date = models.DateField()
    end_date = models.DateField()
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="section_changes")
    reason = models.CharField(max_length=200, blank=True, help_text="For example, classroom maintenance.")

    class Meta:
        ordering = ["-start_date", "section__class_level__order", "section__name"]
        verbose_name = "Temporary classroom change"
        verbose_name_plural = "Temporary classroom changes"

    def __str__(self):
        return f"{self.section}: {self.room} ({self.start_date} to {self.end_date})"

    def clean(self):
        from datetime import timedelta

        from django.core.exceptions import ValidationError

        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})
        if not (self.section_id and self.start_date and self.end_date):
            return
        overlap = SectionRoomChange.objects.filter(
            section_id=self.section_id, start_date__lte=self.end_date, end_date__gte=self.start_date
        ).exclude(pk=self.pk)
        if overlap.exists():
            raise ValidationError({"start_date": "This section already has a classroom change on these dates."})
        if (self.end_date - self.start_date).days > 366:
            raise ValidationError({"end_date": "Choose dates within one year."})
        if not self.room_id:
            return

        from .services import room_for_slot

        slots = list(RoutineSlot.objects.filter(school=self.school).select_related(
            "academic_year", "period", "section__default_room", "room"
        ))
        changes = {}
        for row in SectionRoomChange.objects.filter(
            school=self.school, start_date__lte=self.end_date, end_date__gte=self.start_date
        ).exclude(pk=self.pk).select_related("room"):
            changes.setdefault(row.section_id, []).append(row)
        timings = day_times(self.school)
        for offset in range((self.end_date - self.start_date).days + 1):
            day = self.start_date + timedelta(days=offset)
            if day.isoweekday() in self.school.weekend_day_numbers:
                continue
            active = [
                slot for slot in slots
                if slot.weekday == day.isoweekday()
                and slot.academic_year.start_date <= day <= slot.academic_year.end_date
            ]
            for own in active:
                if own.section_id != self.section_id or own.room_id:
                    continue  # Gym/Lab lessons keep their explicit rooms.
                own_times = times_on(own.period, own.weekday, timings)
                if own_times is None:
                    continue
                for other in active:
                    if other.section_id == self.section_id:
                        continue
                    other_times = times_on(other.period, other.weekday, timings)
                    if other_times is None or not (other_times[0] < own_times[1] and own_times[0] < other_times[1]):
                        continue
                    other_room = room_for_slot(other, day, changes)
                    if other_room and other_room.pk == self.room_id:
                        raise ValidationError({
                            "room": f"{self.room} is used by {other.section} on {day:%d %b %Y}."
                        })


class RoutineSlot(SchoolScopedModel):
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="routine_slots")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="routine_slots")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    period = models.ForeignKey(Period, on_delete=models.CASCADE, related_name="routine_slots")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="routine_slots")
    teacher = models.ForeignKey(
        Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="routine_slots"
    )
    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.SET_NULL, related_name="routine_slots")

    class Meta:
        ordering = ["weekday", "period__order"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "section", "weekday", "period"], name="one_subject_per_section_slot"
            ),
        ]

    def __str__(self):
        return f"{self.section} {self.get_weekday_display()} {self.period}: {self.subject}"

    @property
    def regular_room(self):
        """The repeating room: an explicit Gym/Lab choice, then the section classroom."""
        return self.room or self.section.default_room

    def clean(self):
        """Teacher and room conflict detection across all sections in the same slot."""
        super().clean()
        if not (self.academic_year_id and self.period_id and self.weekday):
            return
        if self.period.is_break:
            raise ValidationError({"period": "Cannot schedule teaching during a break."})
        school = self.period.school
        day = dict(WEEKDAYS).get(self.weekday, "that day")
        if self.weekday in school.weekend_day_numbers:
            raise ValidationError({"weekday": f"The school does not meet on {day}."})
        if self.section_id and self.section.shift:
            has_own_periods = Period.objects.filter(school=school, shift=self.section.shift, is_active=True).exists()
            if self.period.shift != self.section.shift and (self.period.shift or has_own_periods) and not self.pk:
                raise ValidationError({"period": f"Choose a period for the {self.section.get_shift_display()} shift."})
        elif self.period.shift and not self.pk:
            raise ValidationError({"period": "Choose a default period for a section without a shift."})
        # Busy means busy at that time on that day, which a day's own timings can change.
        timings = day_times(school)
        mine = times_on(self.period, self.weekday, timings)
        if mine is None:
            raise ValidationError({"period": f"{self.period.name} is not held on {day}."})
        start, end = mine
        same_day = (
            RoutineSlot.objects.filter(academic_year_id=self.academic_year_id, weekday=self.weekday)
            .exclude(pk=self.pk)
            .select_related("period", "section__class_level")
        )
        overlapping = []
        for other in same_day:
            theirs = times_on(other.period, other.weekday, timings)
            if theirs and theirs[0] < end and start < theirs[1]:
                overlapping.append(other)
        others = RoutineSlot.objects.filter(pk__in=[other.pk for other in overlapping])
        if self.section_id and others.filter(section_id=self.section_id).exists():
            raise ValidationError({"section": "This section already has a lesson during this time."})
        if self.teacher_id:
            clash = others.filter(teacher_id=self.teacher_id).select_related("section__class_level").first()
            if clash:
                raise ValidationError({"teacher": f"{self.teacher} already teaches {clash.section} in this period."})
        room_id = self.room_id or (self.section.default_room_id if self.section_id else None)
        if room_id:
            clash = next(
                (
                    other for other in overlapping
                    if (other.room_id or other.section.default_room_id) == room_id
                ),
                None,
            )
            if clash:
                raise ValidationError({"room": f"Room {self.regular_room} is used by {clash.section} in this period."})
        if overlapping and self.section_id:
            from datetime import timedelta

            from .services import room_for_slot

            section_ids = {self.section_id, *(other.section_id for other in overlapping)}
            changes = {}
            days = set()
            for change in SectionRoomChange.objects.filter(
                school=school, section_id__in=section_ids,
                start_date__lte=self.academic_year.end_date,
                end_date__gte=self.academic_year.start_date,
            ).select_related("room"):
                changes.setdefault(change.section_id, []).append(change)
                first = max(change.start_date, self.academic_year.start_date)
                last = min(change.end_date, self.academic_year.end_date)
                day = first + timedelta(days=(self.weekday - first.isoweekday()) % 7)
                while day <= last:
                    days.add(day)
                    day += timedelta(days=7)
            for day in days:
                mine_room = room_for_slot(self, day, changes)
                if mine_room is None:
                    continue
                for other in overlapping:
                    other_room = room_for_slot(other, day, changes)
                    if other_room and other_room.pk == mine_room.pk:
                        raise ValidationError({
                            "room": f"Room {mine_room} is already used by {other.section} on {day:%d %b %Y}."
                        })
