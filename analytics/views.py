"""
Analytics screens. Each one asks `analytics.access.scope_for` what the viewer may see and shows
nothing beyond it.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext

from core.access import require_permission
from core.charts import bullet_chart, line_chart

from .access import scope_for
from .progress import student_progress


def progress_context(data, student_label):
    """The charts and figures a progress page shows, for the family portal and for staff alike."""
    if data is None:
        return {"progress": None}
    latest = data["latest_subjects"]
    trend = data["trends"]
    overall_series = []
    if data["overall_shown"]:
        overall_series = [(student_label, trend["overall"]), (gettext("Class average"), trend["average"])]
    return {
        "progress": data,
        "subject_chart": bullet_chart(
            [(row["subject"], row["percent"], row["average"], row["highest"]) for row in latest],
            gettext("Each subject in the latest exam against the class"),
            labels=(student_label, gettext("Class average"), gettext("Class highest")),
        )
        if latest
        else "",
        "overall_chart": line_chart(trend["labels"], overall_series, gettext("Overall percentage in each exam"))
        if overall_series and len(trend["labels"]) > 1
        else "",
        "subjects_chart": line_chart(trend["labels"], trend["subjects"], gettext("Each subject in each exam"))
        if trend["subjects"] and len(trend["labels"]) > 1
        else "",
    }


@require_permission(
    None, also="within the viewer's analytics: the whole school, their sections or subjects, or their own child"
)
def student(request, pk):
    """One student's progress, as far as the viewer's analytics reach."""
    from students.models import Student

    from .pdf import progress_pdf

    student = get_object_or_404(Student, school=request.school, pk=pk)
    scope = scope_for(request.user, request.school)
    if scope.empty:
        raise PermissionDenied
    data = student_progress(student, scope)
    if data is None:
        raise Http404("No published result of this student is within what you may see.")
    if request.GET.get("format") == "pdf":
        return progress_pdf(request.school, data)
    return render(
        request,
        "analytics/student.html",
        {
            **progress_context(data, student.first_name),
            "student": student,
            "page_title": gettext("Progress · %(name)s") % {"name": student.full_name},
        },
    )
