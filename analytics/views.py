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


def _exam_choices(school, scope):
    """The current year's exams the viewer has something to analyse in, newest first."""
    from academics.models import AcademicYear
    from examinations.models import Exam

    year = AcademicYear.current_for(school)
    return list(Exam.objects.filter(school=school, academic_year=year).order_by("-end_date", "-pk")) if year else []


@require_permission(None, also="what the viewer's analytics reach: their school, sections or subjects")
def home(request):
    """What this person can analyse in one exam: subjects they teach, sections they lead, the whole school."""
    from django.shortcuts import redirect

    from academics.models import Section
    from examinations.models import ExamSchedule

    scope = scope_for(request.user, request.school)
    if scope.family:
        return redirect("portal:progress")
    if scope.empty:
        raise PermissionDenied
    exams = _exam_choices(request.school, scope)
    chosen = next((e for e in exams if str(e.pk) == request.GET.get("exam")), None) or next(
        (e for e in exams if e.status == "published"), exams[0] if exams else None
    )
    papers, sections = [], []
    if chosen is not None:
        schedules = ExamSchedule.objects.filter(exam=chosen).select_related("subject__combines_into", "class_level")
        units = {}
        for schedule in schedules:
            unit = schedule.subject.combines_into or schedule.subject
            units.setdefault((unit.pk, schedule.class_level_id), (unit, schedule.class_level))
        levels = {level_id for _unit, level_id in units}
        all_sections = list(
            Section.objects.filter(school=request.school, class_level_id__in=levels).order_by(
                "class_level__order", "name"
            )
        )
        for (unit_pk, level_id), (unit, level) in sorted(units.items(), key=lambda i: (i[1][1].order, i[1][0].name)):
            mine = [s for s in all_sections if s.class_level_id == level_id and scope.sees_subject(s.pk, unit_pk)]
            if mine:
                papers.append({"subject": unit, "level": level, "sections": mine})
        sections = [s for s in all_sections if scope.whole_school or s.pk in scope.sections]
    return render(
        request,
        "analytics/home.html",
        {
            "exams": exams,
            "chosen": chosen,
            "papers": papers,
            "sections": sections,
            "whole_school": scope.whole_school,
            "page_title": gettext("Analytics"),
        },
    )


@require_permission(None, also="the subject's teachers for their sections, class teachers, and managers")
def paper(request):
    """One subject in one exam, for the teachers who teach it."""
    from academics.models import Subject
    from core.charts import bars, histogram
    from core.exports import spreadsheet
    from examinations.models import Exam

    from .paper import allowed_sections, paper_analysis

    scope = scope_for(request.user, request.school)
    if scope.empty or scope.family:
        raise PermissionDenied
    exam = get_object_or_404(Exam, school=request.school, pk=request.GET.get("exam") or 0)
    subject = get_object_or_404(Subject, school=request.school, pk=request.GET.get("subject") or 0)
    sections = allowed_sections(exam, subject, scope)
    wanted = request.GET.get("section")
    if wanted:
        sections = [s for s in sections if str(s.pk) == wanted]
    if not sections:
        raise Http404("You do not teach this subject in this exam's classes.")
    data = paper_analysis(exam, subject, sections)
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx"):
        return spreadsheet(
            f"{subject.name}-{exam.name}",
            [
                "Section",
                "Roll",
                "Student",
                "Marks",
                "Out of",
                "Percent",
                "Grade",
                "Previous percent",
                "Change",
                "Follow up",
            ],
            [
                [
                    str(r["section"]),
                    r["enrollment"].roll_number,
                    r["enrollment"].student.full_name,
                    "ABS" if r["absent"] else ("" if r["score"] is None else r["score"]),
                    r["full"],
                    r["percent"],
                    r["letter"],
                    r["before"],
                    r["change"],
                    ", ".join(r["flags"]),
                ]
                for r in data["rows"]
            ],
            fmt,
        )
    return render(
        request,
        "analytics/paper.html",
        {
            "data": data,
            "all_sections": allowed_sections(exam, subject, scope),
            "wanted": wanted or "",
            "histogram": histogram(data["bins"], gettext("How the marks spread (percent)")),
            "sections_chart": bars(
                [(str(s["section"]), s["average"], f"{s['average']}% · {s['sat']}") for s in data["sections"]],
                gettext("Average in each section"),
            )
            if len(data["sections"]) > 1
            else "",
            "parts_chart": bars(
                [(name, average, f"{average}%") for name, average, _count in data["parts"]],
                gettext("Average in each part of the paper"),
            )
            if data["parts"]
            else "",
            "page_title": f"{subject.name} · {exam.name}",
        },
    )


@require_permission(None, also="the section's class teacher, and managers")
def section(request):
    """A class teacher's grid: every student against every subject in one published exam."""
    from academics.models import Section
    from core.exports import spreadsheet
    from examinations.models import Exam

    from .paper import section_grid

    scope = scope_for(request.user, request.school)
    exam = get_object_or_404(Exam, school=request.school, pk=request.GET.get("exam") or 0)
    target = get_object_or_404(Section, school=request.school, pk=request.GET.get("section") or 0)
    if not (scope.whole_school or (target.pk in scope.sections and exam.academic_year_id == scope.year_id)):
        raise PermissionDenied
    grid = section_grid(exam, target)
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx"):
        headers = ["Roll", "Student", *[name for _pk, name in grid["columns"]], "Percent", "GPA or grades", "Position"]
        body = [
            [
                row["fact"].enrollment.roll_number,
                row["fact"].enrollment.student.full_name,
                *[cell.percent if cell else "" for cell in row["cells"]],
                row["fact"].percent,
                row["fact"].gpa if row["fact"].gpa is not None else row["fact"].headline,
                row["fact"].section_rank if row["fact"].show_rank else "",
            ]
            for row in grid["rows"]
        ]
        return spreadsheet(f"{target}-{exam.name}", headers, body, fmt)
    return render(
        request,
        "analytics/section.html",
        {"grid": grid, "page_title": f"{target} · {exam.name}"},
    )
