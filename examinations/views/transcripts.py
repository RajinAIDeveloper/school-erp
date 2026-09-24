"""Transcripts: the school's queue of requests, issuing one, the printed copy, and the public check."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.access import require_permission

from .. import transcripts as service
from ..models import Transcript, TranscriptRequest


def verify_link(request, transcript):
    return request.build_absolute_uri(reverse("examinations:transcript_verify", args=[transcript.verification_code]))


def _errors(exc):
    return " ".join(exc.messages) if hasattr(exc, "messages") else str(exc)


@require_permission("examinations.view_transcript", also="the school's managers")
def transcript_queue(request):
    """Requests waiting, newest first, and the transcripts issued lately with any that need a look."""
    waiting = (
        TranscriptRequest.objects.filter(school=request.school, status=TranscriptRequest.Status.REQUESTED)
        .select_related("student", "requested_by")
        .order_by("created_at")
    )
    recent = list(
        Transcript.objects.filter(school=request.school)
        .select_related("student", "issued_by")
        .order_by("-created_at")[:30]
    )
    for item in recent:
        item.stale = service.staleness(item) if not item.is_revoked else {"corrected": [], "newer": []}
    return render(
        request,
        "examinations/transcripts.html",
        {"waiting": waiting, "recent": recent, "page_title": "Transcripts"},
    )


@require_permission("examinations.view_transcript", also="the school's managers")
def student_transcripts(request, pk):
    """One student's transcripts: a preview of what would be issued, the choices, and those already issued."""
    from students.models import Student

    student = get_object_or_404(Student, school=request.school, pk=pk)
    years = service.candidate_years(student)
    source = request.POST if request.method == "POST" else request.GET
    choices = {}
    for entry in years:
        wanted = source.get(f"year_{entry['year'].pk}")
        if wanted and any(choice[0] == wanted for choice in entry["choices"]):
            choices[entry["year"].pk] = wanted
    # Both are on until the issuer changes them; the form marks itself with "options".
    chosen_options = "options" in source
    with_percent = bool(source.get("percent")) if chosen_options else True
    with_predicted = bool(source.get("predicted")) if chosen_options else True
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "issue":
                transcript = service.issue_transcript(
                    school=request.school,
                    user=request.user,
                    student=student,
                    choices=choices,
                    with_percent=with_percent,
                    with_predicted=with_predicted,
                )
                messages.success(request, f"Transcript {transcript.serial} issued.")
                owed = service._outstanding(student)
                if owed > 0:
                    messages.warning(request, f"{student} still owes {owed} in fees. The transcript was issued anyway.")
            elif action in ("revoke", "reissue"):
                item = get_object_or_404(
                    Transcript, school=request.school, student=student, pk=request.POST.get("transcript") or 0
                )
                if action == "revoke":
                    service.revoke_transcript(
                        school=request.school, user=request.user, transcript=item, reason=request.POST.get("reason", "")
                    )
                    messages.success(request, f"{item.serial} revoked.")
                else:
                    new = service.reissue_transcript(school=request.school, user=request.user, transcript=item)
                    messages.success(request, f"{new.serial} issued in place of {item.serial}.")
            elif action == "decline":
                item = get_object_or_404(
                    TranscriptRequest, school=request.school, student=student, pk=request.POST.get("request") or 0
                )
                service.decline_request(
                    school=request.school, user=request.user, request_item=item, reason=request.POST.get("reason", "")
                )
                messages.success(request, "Request declined. The family sees the reason.")
        except ValidationError as exc:
            messages.error(request, _errors(exc))
        return redirect("examinations:student_transcripts", pk=student.pk)
    preview, _sources = service.build(
        student, choices=choices, with_percent=with_percent, with_predicted=with_predicted
    )
    issued = list(student.transcripts.select_related("issued_by", "replaces").order_by("-created_at"))
    for item in issued:
        item.stale = service.staleness(item) if not item.is_revoked else {"corrected": [], "newer": []}
    return render(
        request,
        "examinations/student_transcripts.html",
        {
            "student": student,
            "years": [{**entry, "chosen": choices.get(entry["year"].pk, entry["default"])} for entry in years],
            "preview": preview,
            "with_percent": with_percent,
            "with_predicted": with_predicted,
            "issued": issued,
            "waiting": student.transcript_requests.filter(status=TranscriptRequest.Status.REQUESTED).first(),
            "owed": service._outstanding(student),
            "page_title": f"Transcripts · {student.full_name}",
        },
    )


@require_permission("examinations.view_transcript", also="the school's managers")
def transcript_download(request, pk):
    from ..transcript_pdf import transcript_pdf

    item = get_object_or_404(Transcript.objects.select_related("school"), school=request.school, pk=pk)
    return transcript_pdf(item, verify_link(request, item))


def transcript_verify(request, code):
    """
    Public check of a transcript. It shows the grades so the reader can compare them with the
    paper, the student only as initials, and never the date of birth, parents, IDs or marks.
    """
    item = get_object_or_404(Transcript.objects.select_related("school", "student"), verification_code=code)
    replacement = getattr(item, "replaced_by", None)
    data = item.payload
    response = render(
        request,
        "examinations/transcript_verify.html",
        {
            "transcript": item,
            "school": item.school,
            "initials": service.masked(data.get("student", {}).get("name", "")),
            "years": data.get("years", []),
            "board": data.get("board", []),
            "replacement": replacement,
            "corrected": [] if item.is_revoked else service.staleness(item)["corrected"],
        },
    )
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex"
    return response
