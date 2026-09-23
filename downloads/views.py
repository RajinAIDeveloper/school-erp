"""Notices and downloadable material, both behind the same audience rules."""

from django.db.models import F
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.access import require_permission
from core.models import audit

from .forms import DownloadForm
from .models import Audience, DownloadItem, live_notices_for


@require_permission("downloads.view_downloaditem")
def listing(request):
    items = [
        item
        for item in DownloadItem.objects.filter(school=request.school, is_active=True).select_related("category")
        if item.visible_to(request.user)
    ]
    return render(
        request,
        "downloads/list.html",
        {
            "items": items,
            "notices": live_notices_for(request.user, request.school)[:5],
            "page_title": "Downloads",
        },
    )


@require_permission("downloads.view_downloaditem")
def notices(request):
    """The school's noticeboard, as seen by whoever is looking at it."""
    return render(
        request,
        "downloads/notices.html",
        {"notices": live_notices_for(request.user, request.school), "page_title": "Notices"},
    )


def download(request, pk):
    obj = get_object_or_404(DownloadItem, pk=pk, is_active=True)
    if obj.audience != Audience.PUBLIC and (
        not request.user.is_authenticated or not request.user.has_perm("downloads.view_downloaditem")
    ):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    if request.user.is_authenticated and request.school is not None and obj.school_id != request.school.pk:
        from django.http import Http404

        raise Http404
    if not obj.visible_to(request.user):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied
    response = FileResponse(obj.file.open("rb"), as_attachment=True, filename=obj.filename)
    DownloadItem.objects.filter(pk=pk).update(download_count=F("download_count") + 1)
    return response


@require_permission("downloads.add_downloaditem")
def create(request):
    form = DownloadForm(request.POST or None, request.FILES or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.instance.uploaded_by = request.user
        item = form.save()
        audit(request, "download.uploaded", item, item.title)
        return redirect("downloads:list")
    return render(request, "generic/form.html", {"form": form, "page_title": "Upload learning material"})
