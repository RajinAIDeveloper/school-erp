"""Small JSON endpoints that keep dependent selects (class -> section) tenant scoped."""

from django.http import JsonResponse

from core.access import require_permission

from .models import Section


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
