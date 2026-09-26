"""School subject plans and optional dated teaching plans."""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from core.access import is_manager, require_permission
from core.models import audit
from core.modules import has_module

from .forms import SubjectAssignmentForm, TeachingPlanItemForm
from .models import AcademicYear, ClassLevel, ClassSubject, Section, Subject, SubjectTeacher, TeachingPlanItem, Term


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
def subjects(request):
    """Subject catalogue and the class/section plans reachable by this user."""
    year = _pick(AcademicYear.objects.filter(school=request.school), request.GET.get("year"))
    year = year or AcademicYear.current_for(request.school)
    manager = is_manager(request.user)
    rows = ClassSubject.objects.filter(school=request.school, academic_year=year).select_related(
        "subject", "class_level"
    ) if year else ClassSubject.objects.none()
    assignments = SubjectTeacher.objects.filter(school=request.school, academic_year=year).select_related(
        "subject", "section__class_level", "teacher"
    ) if year else SubjectTeacher.objects.none()
    if not manager:
        assignments = assignments.filter(teacher__user=request.user)
        taught = Q(pk__in=[])
        for class_level_id, subject_id in assignments.values_list("section__class_level_id", "subject_id"):
            taught |= Q(class_level_id=class_level_id, subject_id=subject_id)
        rows = rows.filter(taught)
    return render(request, "academics/subjects.html", {
        "page_title": "Subjects", "manager": manager, "year": year,
        "years": AcademicYear.objects.filter(school=request.school),
        "subjects": Subject.objects.filter(school=request.school) if manager else [],
        "rows": rows, "assignments": assignments,
    })


@require_permission("academics.view_classsubject", also="managers only")
def subject_detail(request, pk):
    if not is_manager(request.user):
        raise PermissionDenied
    subject = get_object_or_404(Subject, school=request.school, pk=pk)
    year = _pick(AcademicYear.objects.filter(school=request.school), request.GET.get("year"))
    year = year or AcademicYear.current_for(request.school)
    plans = ClassSubject.objects.filter(school=request.school, academic_year=year, subject=subject).select_related(
        "class_level"
    ) if year else ClassSubject.objects.none()
    plan_by_level = {row.class_level_id: row for row in plans}
    assigned = list(SubjectTeacher.objects.filter(
        school=request.school, academic_year=year, subject=subject
    ).select_related("teacher", "section")) if year else []
    assigned_by_section = {}
    for row in assigned:
        assigned_by_section.setdefault(row.section_id, []).append(row)
    levels = ClassLevel.objects.filter(school=request.school, is_active=True).prefetch_related("sections")
    coverage = []
    for level in levels:
        sections = []
        for section in level.sections.all():
            if section.is_active:
                sections.append({"section": section, "teachers": assigned_by_section.get(section.pk, [])})
        coverage.append({"level": level, "plan": plan_by_level.get(level.pk), "sections": sections})
    return render(request, "academics/subject_detail.html", {
        "page_title": subject.name, "subject": subject, "year": year,
        "years": AcademicYear.objects.filter(school=request.school), "coverage": coverage,
    })


@require_permission("academics.add_subjectteacher")
def subject_teacher_create(request):
    """Assign a teacher, creating the missing class subject row in the same transaction."""
    initial = {}
    for field in ("academic_year", "section", "subject", "teacher"):
        if request.GET.get(field, "").isdigit():
            initial[field] = request.GET[field]
    if "academic_year" not in initial:
        current = AcademicYear.current_for(request.school)
        if current:
            initial["academic_year"] = current.pk
    form = SubjectAssignmentForm(request.POST or None, school=request.school, initial=initial)
    return_to = request.GET.get("return_to", "")
    if not (return_to.startswith("/") and not return_to.startswith("//") and url_has_allowed_host_and_scheme(
        return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    )):
        return_to = reverse("academics:subjects")
    if request.method == "POST" and form.is_valid():
        year = form.cleaned_data["academic_year"]
        section = form.cleaned_data["section"]
        subject = form.cleaned_data["subject"]
        teacher = form.cleaned_data["teacher"]
        with transaction.atomic():
            if not subject.class_levels.filter(pk=section.class_level_id).exists():
                subject.class_levels.add(section.class_level)
            plan = ClassSubject.objects.filter(
                school=request.school, academic_year=year, class_level=section.class_level, subject=subject
            ).first()
            if plan is None:
                plan = ClassSubject.objects.create(
                    school=request.school, academic_year=year, class_level=section.class_level,
                    subject=subject, kind=form.cleaned_data["kind"],
                )
            assignment = SubjectTeacher(
                school=request.school, academic_year=year, section=section, subject=subject, teacher=teacher
            )
            assignment.full_clean()
            assignment.save()
            audit(request, "record.saved", assignment)
        messages.success(request, f"{teacher} now teaches {subject} in {section}. The class subject plan is ready too.")
        return redirect(return_to)
    return render(request, "academics/subject_teacher_form.html", {
        "page_title": "Assign a subject teacher", "form": form, "cancel_url": return_to,
    })


@require_permission("academics.view_classsubject")
def subject_plan(request):
    """
    One class's subjects for one year, grouped as the school teaches them, with the two
    shortcuts most schools need: copy last year's plan, or start from the national Classes
    9-10 plan.
    """
    from .presets import copy_plan, load_national_plan

    years = AcademicYear.objects.filter(school=request.school)
    if not is_manager(request.user):
        years = years.filter(subject_teachers__teacher__user=request.user).distinct()
    data = request.POST if request.method == "POST" else request.GET
    year = _pick(years, data.get("year")) or _pick(years, getattr(AcademicYear.current_for(request.school), "pk", None)) or years.first()
    levels = ClassLevel.objects.filter(school=request.school, is_active=True)
    if not is_manager(request.user):
        levels = levels.filter(
            sections__subject_teachers__teacher__user=request.user,
            sections__subject_teachers__academic_year=year,
        ).distinct() if year else levels.none()
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
    if not is_manager(request.user) and year and level:
        taught = SubjectTeacher.objects.filter(
            school=request.school, academic_year=year,
            section__class_level=level, teacher__user=request.user,
        ).values("subject_id")
        rows = rows.filter(subject_id__in=taught)
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


def _subject_setup_scope(request, row):
    """A manager plans the class; a teacher plans only their assigned sections."""
    sections = Section.objects.filter(school=request.school, class_level=row.class_level, is_active=True)
    manager = is_manager(request.user)
    if not manager:
        sections = sections.filter(
            subject_teachers__academic_year=row.academic_year,
            subject_teachers__subject=row.subject,
            subject_teachers__teacher__user=request.user,
        ).distinct()
        if not sections.exists():
            raise PermissionDenied("You are not assigned to teach this subject in this class.")
    return sections, manager


def _subject_setup_row(request, pk):
    return get_object_or_404(
        ClassSubject.objects.select_related("academic_year", "class_level", "subject"),
        school=request.school,
        pk=pk,
    )


def _setup_url(name, **params):
    return reverse(name) + ("?" + urlencode(params) if params else "")


@require_permission("academics.view_teachingplanitem", also="assigned subject unless a manager")
def subject_setup(request, pk):
    """A guided continuation after adding a subject, with optional planning in one place."""
    from examinations.models import Exam, ExamSchedule
    from timetable.models import RoutineSlot

    row = _subject_setup_row(request, pk)
    sections, manager = _subject_setup_scope(request, row)
    hub_url = reverse("academics:subject_setup", args=[row.pk])
    terms = Term.objects.filter(school=request.school, academic_year=row.academic_year)
    selected_term = get_object_or_404(terms, pk=request.GET["term"]) if request.GET.get("term", "").isdigit() else None
    today = timezone.localdate()
    if selected_term:
        initial_date = today if selected_term.start_date <= today <= selected_term.end_date else selected_term.start_date
    else:
        initial_date = today if row.academic_year.start_date <= today <= row.academic_year.end_date else row.academic_year.start_date
    form = TeachingPlanItemForm(
        request.POST if request.method == "POST" else None,
        school=request.school,
        subject_row=row,
        allowed_sections=sections,
        may_plan_all=manager,
        initial={"term": selected_term.pk if selected_term else None, "planned_date": initial_date},
    ) if request.user.has_perm("academics.add_teachingplanitem") else None
    if request.method == "POST":
        if form is None:
            raise PermissionDenied
        if form.is_valid():
            item = form.save()
            audit(request, "teaching_plan.created", item, str(item))
            messages.success(request, "Teaching plan item added. You can add another or continue setup.")
            return redirect(hub_url + (f"?term={selected_term.pk}" if selected_term else ""))

    assignments = SubjectTeacher.objects.filter(
        school=request.school, academic_year=row.academic_year, section__in=sections, subject=row.subject
    ).select_related("teacher")
    slots = RoutineSlot.objects.filter(
        school=request.school, academic_year=row.academic_year, section__in=sections, subject=row.subject
    ).select_related("period", "teacher").order_by("weekday", "period__order")
    section_rows = []
    for section in sections:
        section_assignments = [assignment for assignment in assignments if assignment.section_id == section.pk]
        section_slots = [slot for slot in slots if slot.section_id == section.pk]
        section_rows.append({
            "section": section,
            "teachers": section_assignments,
            "has_teacher": bool(section_assignments),
            "slots": section_slots,
            "assign_url": _setup_url(
                "settings:subject_teacher_create", academic_year=row.academic_year_id,
                section=section.pk, subject=row.subject_id, return_to=hub_url,
            ),
            "routine_url": _setup_url(
                "timetable:grid_edit", academic_year=row.academic_year_id,
                section=section.pk, return_to=hub_url,
            ),
        })

    items = TeachingPlanItem.objects.filter(
        school=request.school, academic_year=row.academic_year,
        class_level=row.class_level, subject=row.subject,
    ).filter(Q(section__isnull=True) | Q(section__in=sections)).select_related("term", "section")
    if selected_term:
        items = items.filter(term=selected_term)
    editable_section_ids = set(sections.values_list("pk", flat=True))
    items = list(items)
    for item in items:
        item.can_edit_here = manager or item.section_id in editable_section_ids
    papers = ExamSchedule.objects.filter(
        school=request.school, exam__academic_year=row.academic_year,
        class_level=row.class_level, subject=row.subject,
    ).select_related("exam").order_by("date")
    if selected_term:
        papers = papers.filter(exam__term=selected_term)
    exams = Exam.objects.filter(school=request.school, academic_year=row.academic_year).order_by("start_date", "name")
    if selected_term:
        exams = exams.filter(term=selected_term)
    scheduled_exam_ids = set(papers.values_list("exam_id", flat=True))
    exam_rows = [{
        "exam": exam,
        "has_paper": exam.pk in scheduled_exam_ids,
        "schedule_url": _setup_url(
            "examinations:schedule_create", exam=exam.pk, class_level=row.class_level_id,
            subject=row.subject_id, return_to=hub_url,
        ),
    } for exam in exams]
    homework_enabled = has_module(request.school, "homework") and row.academic_year_id == getattr(
        AcademicYear.current_for(request.school), "pk", None
    )
    return render(request, "academics/subject_setup.html", {
        "row": row,
        "page_title": f"Set up {row.subject} for {row.class_level}",
        "section_rows": section_rows,
        "terms": terms,
        "selected_term": selected_term,
        "form": form,
        "items": items,
        "papers": papers,
        "exam_rows": exam_rows,
        "exam_create_url": _setup_url(
            "examinations:exam_create", academic_year=row.academic_year_id,
            **({"term": selected_term.pk} if selected_term else {}), return_to=hub_url,
        ),
        "homework_enabled": homework_enabled,
        "homework_url": _setup_url(
            "homework:create", unit=f"{row.class_level_id}-{row.subject_id}", return_to=hub_url,
        ),
        "subject_plan_url": _setup_url(
            "academics:subject_plan", year=row.academic_year_id, class_level=row.class_level_id,
        ),
        "term_create_url": _setup_url(
            "settings:term_create", academic_year=row.academic_year_id, return_to=hub_url,
        ),
        "section_create_url": _setup_url(
            "settings:section_create", class_level=row.class_level_id, return_to=hub_url,
        ),
        "manager": manager,
    })


@require_permission("academics.change_teachingplanitem", also="own assigned section unless a manager")
def teaching_plan_edit(request, pk):
    item = get_object_or_404(
        TeachingPlanItem.objects.select_related("academic_year", "class_level", "subject", "section"),
        school=request.school, pk=pk,
    )
    row = ClassSubject.objects.filter(
        school=request.school, academic_year=item.academic_year,
        class_level=item.class_level, subject=item.subject,
    ).order_by("pk").first()
    if row is None:
        from django.http import Http404

        raise Http404
    sections, manager = _subject_setup_scope(request, row)
    if not manager and (item.section_id is None or not sections.filter(pk=item.section_id).exists()):
        raise PermissionDenied("Only a school manager can edit a plan for every section.")
    form = TeachingPlanItemForm(
        request.POST if request.method == "POST" else None,
        instance=item, school=request.school, subject_row=row,
        allowed_sections=sections, may_plan_all=manager,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request, "teaching_plan.updated", item, str(item))
        messages.success(request, "Teaching plan updated.")
        return redirect("academics:subject_setup", pk=row.pk)
    return render(request, "generic/form.html", {
        "form": form,
        "page_title": f"Edit teaching plan: {item.topic}",
        "cancel_url": reverse("academics:subject_setup", args=[row.pk]),
    })
