"""Small JSON endpoints that keep dependent selects tenant scoped, and the subject plan screen."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render

from core.access import require_permission

from .models import AcademicYear, ClassLevel, ClassSubject, Section


@require_permission("academics.view_section")
def sections_json(request):
    """Sections of one class in the current school, for dependent <select> widgets."""
    raw = request.GET.get("class_level", "")
    sections = Section.objects.filter(school=request.school).select_related("class_level")
    if raw.isdigit():
        sections = sections.filter(class_level_id=int(raw))
    elif raw:
        sections = sections.none()
    return JsonResponse([{"id": s.pk, "name": str(s)} for s in sections], safe=False)


def _pick(queryset, raw):
    return queryset.filter(pk=int(raw)).first() if str(raw).isdigit() else None


@require_permission("academics.view_classsubject")
def subject_plan(request):
    """
    One class's subjects for one year, grouped as the school teaches them, with the two
    shortcuts most schools need: copy last year's plan, or start from the national Classes
    9-10 plan.
    """
    from .presets import copy_plan, load_national_plan

    years = AcademicYear.objects.filter(school=request.school)
    levels = ClassLevel.objects.filter(school=request.school, is_active=True)
    data = request.POST if request.method == "POST" else request.GET
    year = _pick(years, data.get("year")) or AcademicYear.current_for(request.school)
    level = _pick(levels, data.get("class_level"))

    if request.method == "POST" and year and level:
        if not request.user.has_perm("academics.add_classsubject"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        try:
            if data.get("action") == "national":
                added = load_national_plan(
                    school=request.school, user=request.user, academic_year=year, class_level=level
                )
                messages.success(
                    request,
                    f"Added {added} subject row(s) from the national Classes 9-10 plan. Check the subject codes "
                    "and groups against the board's current list.",
                )
            elif data.get("action") == "copy":
                source = _pick(years, data.get("from_year"))
                if source is None:
                    raise ValidationError("Choose the year to copy from.")
                added = copy_plan(
                    school=request.school, user=request.user, from_year=source, to_year=year, class_level=level
                )
                messages.success(request, f"Copied {added} subject row(s) from {source}.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect(f"{request.path}?year={year.pk}&class_level={level.pk}")

    rows = (
        ClassSubject.objects.filter(school=request.school, academic_year=year, class_level=level)
        .select_related("subject")
        .order_by("group", "kind", "subject__code", "subject__name")
        if year and level
        else ClassSubject.objects.none()
    )
    return render(
        request,
        "academics/subject_plan.html",
        {
            "years": years,
            "levels": levels,
            "year": year,
            "level": level,
            "rows": rows,
            "page_title": "Subject plan",
        },
    )
