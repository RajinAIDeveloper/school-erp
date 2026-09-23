from django.conf import settings
from django.db import models

from core.models import SchoolScopedModel
from employees.models import Employee
from students.models import Enrollment


class AttendanceStatus(models.TextChoices):
    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    LATE = "late", "Late"
    LEAVE = "leave", "Leave"
    HALF_DAY = "half_day", "Half day"


class StudentAttendance(SchoolScopedModel):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name="attendance")
    date = models.DateField()
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices, default=AttendanceStatus.PRESENT)
    remarks = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-date", "enrollment__roll_number"]
        constraints = [
            models.UniqueConstraint(fields=["enrollment", "date"], name="one_student_attendance_per_day"),
        ]
        indexes = [models.Index(fields=["school", "date"])]

    def __str__(self):
        return f"{self.enrollment.student} {self.date} {self.status}"


class StaffAttendance(SchoolScopedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="attendance")
    date = models.DateField()
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices, default=AttendanceStatus.PRESENT)
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    remarks = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_by_leave = models.ForeignKey(
        "attendance.LeaveRequest",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attendance_rows",
        help_text="Set when an approved leave generated this row, so withdrawing the leave removes it again.",
    )

    class Meta:
        ordering = ["-date", "employee__employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "date"], name="one_staff_attendance_per_day"),
        ]
        indexes = [models.Index(fields=["school", "date"])]

    def __str__(self):
        return f"{self.employee} {self.date} {self.status}"


class LeaveType(SchoolScopedModel):
    name = models.CharField(max_length=50, help_text="e.g. Casual, Sick, Earned")
    days_per_year = models.PositiveSmallIntegerField(default=10)
    allow_negative = models.BooleanField(
        default=False,
        help_text="Let a request go past the annual entitlement. The balance is then shown as negative.",
    )

    is_active = models.BooleanField(
        default=True, help_text="Clear this to retire the record without losing the history that uses it."
    )

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_leave_type_per_school")]

    def __str__(self):
        return self.name


class LeaveRequest(SchoolScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT, related_name="requests")
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.employee} {self.leave_type} {self.start_date} - {self.end_date}"

    @property
    def days(self):
        """Working days the request covers: weekends and school holidays cost nothing."""
        from .services import leave_days

        return leave_days(self.school, self.start_date, self.end_date)

    def days_in_year(self, year):
        from .services import leave_days_in_year

        return leave_days_in_year(self.school, self.start_date, self.end_date, year)
