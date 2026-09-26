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
        constraints = [models.UniqueConstraint(fields=["school", "order"], name="unique_period_order_per_school")]

    def __str__(self):
        return f"{self.name} ({self.start_time:%H:%M}-{self.end_time:%H:%M})"


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
        if self.room_id:
            clash = others.filter(room_id=self.room_id).select_related("section__class_level").first()
            if clash:
                raise ValidationError({"room": f"Room {self.room} is used by {clash.section} in this period."})
