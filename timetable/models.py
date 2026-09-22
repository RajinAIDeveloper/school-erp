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
        others = RoutineSlot.objects.filter(
            academic_year_id=self.academic_year_id,
            weekday=self.weekday,
            period__start_time__lt=self.period.end_time,
            period__end_time__gt=self.period.start_time,
        ).exclude(pk=self.pk)
        if self.period.is_break:
            raise ValidationError({"period": "Cannot schedule teaching during a break."})
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
