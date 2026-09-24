"""
Early warning: the students in a section whose attendance or results call for a word now,
while there is still time before the final exam.

A student is listed for any of: attendance below a threshold this year; a result down by a
set number of percentage points since the previous published exam; a subject failed in the
latest published exam; a subject passed by only a few marks. Every figure comes from the
same published results and registers the cards and the attendance reports use.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from examinations.exports import failed_subjects, near_pass_mark
from examinations.models import ResultSnapshot
from examinations.parts import plain
from examinations.rulebooks import rulebook
from examinations.services import class_attendance
from students.models import Enrollment


def early_warnings(section, year, *, attendance_below=75, drop_by=10, margin=5):
    """
    ([{enrollment, attendance, latest, change, concerns}], students) for one section this year,
    listing only the students with at least one concern.
    """
    enrollments = list(
        Enrollment.objects.filter(
            school=section.school,
            section=section,
            academic_year=year,
            status=Enrollment.Status.ENROLLED,
            student__status="active",
        )
        .select_related("student")
        .order_by("roll_number")
    )
    ids = [e.pk for e in enrollments]
    attendance = class_attendance(ids, timezone.localdate())
    results = defaultdict(list)
    snapshots = (
        ResultSnapshot.objects.filter(
            enrollment_id__in=ids, exam__status="published", version=F("exam__publication_version")
        )
        .select_related("exam")
        .order_by("exam__end_date", "exam__published_at", "exam_id")
    )
    for snapshot in snapshots:
        results[snapshot.enrollment_id].append(snapshot)

    flagged = []
    for enrollment in enrollments:
        concerns = []
        figures = attendance.get(enrollment.pk)
        if figures and figures["percent"] < attendance_below:
            concerns.append(f"Attendance {figures['percent']}% this year")
        sat = results[enrollment.pk]
        latest = sat[-1] if sat else None
        previous = sat[-2] if len(sat) > 1 else None
        change = None
        if latest and previous and latest.payload.get("percent") and previous.payload.get("percent"):
            change = Decimal(latest.payload["percent"]) - Decimal(previous.payload["percent"])
            if -change >= drop_by:
                concerns.append(f"Down {plain(-change)} points since {previous.exam.name}")
        if latest and rulebook(latest.payload.get("system") or "own").pass_marks:
            _headers, failed, _counts = failed_subjects([latest.payload])
            if failed:
                concerns.append(f"Failed in {latest.exam.name}: {failed[0][5]}")
            _headers, near = near_pass_mark([latest.payload], margin)
            only_just = [f"{line[0]} ({line[6].lower()})" for line in near if line[6].startswith("Passed by")]
            if only_just:
                concerns.append("Only just passed: " + ", ".join(only_just))
        if concerns:
            flagged.append(
                {
                    "enrollment": enrollment,
                    "attendance": figures["percent"] if figures else None,
                    "latest": latest,
                    "change": change,
                    "concerns": concerns,
                }
            )
    return flagged, len(enrollments)
