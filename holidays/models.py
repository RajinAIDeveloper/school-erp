from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models

from core.models import SchoolScopedModel


class Holiday(SchoolScopedModel):
    class Type(models.TextChoices):
        PUBLIC = "public", "Public holiday"
        RELIGIOUS = "religious", "Religious"
        SCHOOL = "school", "School holiday"
        VACATION = "vacation", "Vacation"
        EVENT = "event", "Event (school open)"

    name = models.CharField(max_length=150)
    holiday_type = models.CharField(max_length=10, choices=Type.choices, default=Type.PUBLIC)
    start_date = models.DateField()
    end_date = models.DateField()
    description = models.TextField(blank=True)
    # "event" type entries do not close the school; everything else excludes attendance.
    closes_school = models.BooleanField(default=True)

    class Meta:
        ordering = ["start_date"]

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError("End date cannot be before start date.")

    def __str__(self):
        return f"{self.name} ({self.start_date:%d %b})"

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    def dates(self):
        d = self.start_date
        while d <= self.end_date:
            yield d
            d += timedelta(days=1)


def is_holiday(school, date):
    """True if the school is closed on `date` (holiday or weekend)."""
    if date.isoweekday() in school.weekend_day_numbers:
        return True
    return Holiday.objects.filter(
        school=school, closes_school=True, start_date__lte=date, end_date__gte=date
    ).exists()


def holiday_dates_between(school, start, end):
    dates = set()
    for h in Holiday.objects.filter(school=school, closes_school=True, start_date__lte=end, end_date__gte=start):
        for d in h.dates():
            if start <= d <= end:
                dates.add(d)
    return dates
