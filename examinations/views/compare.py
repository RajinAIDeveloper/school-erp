"""
Recompute and compare: this system's results for one exam and class, set against what another
program published for the same exam.
"""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import render

from academics.models import ClassLevel
from core.access import require_permission
from core.exports import spreadsheet
from core.forms import TailwindFormMixin

from ..compare import TEMPLATE_HEADERS, compare, template_rows
from ..mark_import import read_rows
from ..models import Exam
from ..services import build_result_sheet


class CompareForm(TailwindFormMixin, forms.Form):
    exam = forms.ModelChoiceField(queryset=None)
    class_level = forms.ModelChoiceField(queryset=None, label="Class")

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exam"].queryset = Exam.objects.filter(school=school).select_related("academic_year")
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)


@require_permission("examinations.change_exam")
def compare_results(request):
    """
    Upload the old software's results for an exam and see every mark, grade and GPA that does
    not match this system's. Nothing is saved.
    """
    form = CompareForm(request.POST or request.GET or None, school=request.school)
    context = {"form": form, "page_title": "Compare with another program"}
    if not (form.is_bound and form.is_valid()):
        return render(request, "examinations/compare.html", context)
    exam, level = form.cleaned_data["exam"], form.cleaned_data["class_level"]
    rows = build_result_sheet(exam, level)["rows"]
    action = request.POST.get("action") or request.GET.get("action")
    if action in ("template_xlsx", "template_csv"):
        return spreadsheet(
            f"compare-{exam.name}-{level.name}",
            TEMPLATE_HEADERS,
            template_rows(rows),
            action.rsplit("_", 1)[1],
            preamble=[
                ("Exam", f"{exam.name} ({exam.academic_year})"),
                ("Class", str(level)),
                ("How to fill it", "Copy in the marks, grades and GPA your old software published for this exam."),
            ],
        )
    if request.method == "POST" and action in ("compare", "compare_csv"):
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose the file of results to compare.")
        else:
            try:
                differences, summary = compare(rows, read_rows(upload))
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                context.update(differences=differences, summary=summary, filename=upload.name, exam=exam, level=level)
                if action == "compare_csv":
                    return spreadsheet(
                        "differences",
                        ["Row", "Student", "Subject", "What", "Here", "In the file"],
                        [[d["row"], d["student"], d["subject"], d["what"], d["here"], d["there"]] for d in differences],
                        "csv",
                    )
    context.update(students=len(rows), exam=exam, level=level)
    return render(request, "examinations/compare.html", context)
