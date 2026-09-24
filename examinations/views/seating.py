"""
Seat plans: one per sitting of an exam, printed as door lists, seat stickers and invigilator
attendance sheets.
"""

from datetime import date, datetime
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.access import require_permission

from ..models import Exam, SeatPlan
from ..seating import build_plan, candidates, room_lists, set_locked, sittings


@require_permission("examinations.change_exam")
def seat_plans(request, pk):
    """Every sitting of an exam, how many students sit it, and whether they have seats yet."""
    exam = get_object_or_404(Exam, school=request.school, pk=pk)
    plans = {(p.date, p.start_time): p for p in exam.seat_plans.annotate(seated=Count("seats"))}
    rows = [
        {
            "date": day,
            "start": start,
            "papers": papers,
            "students": len(candidates(exam, papers)),
            "plan": plans.get((day, start)),
        }
        for day, start, papers in sittings(exam)
    ]
    undated = (
        exam.schedules.filter(date__isnull=True).count()
        + exam.schedules.filter(date__isnull=False, start_time__isnull=True).count()
    )
    return render(
        request,
        "examinations/seat_plans.html",
        {"exam": exam, "rows": rows, "undated": undated, "page_title": f"Seat plans · {exam.name}"},
    )


def _sitting(request, exam):
    data = request.POST if request.method == "POST" else request.GET
    try:
        day = date.fromisoformat(data.get("date", ""))
        start = datetime.strptime(data.get("time", ""), "%H:%M").time()
    except ValueError:
        raise Http404("Choose a sitting from the exam's seat plans.") from None
    papers = next((p for d, s, p in sittings(exam) if (d, s) == (day, start)), None)
    if papers is None:
        raise Http404("No paper of this exam starts at that date and time.")
    return day, start, papers


@require_permission("examinations.change_exam")
def seat_plan(request, pk):
    """Seat one sitting's students in the rooms chosen, lock the plan once printed, and print it."""
    from timetable.models import Room

    from ..documents import SEAT_PLAN_PRINTS, seat_plan_pdf

    exam = get_object_or_404(Exam, school=request.school, pk=pk)
    day, start, papers = _sitting(request, exam)
    plan = SeatPlan.objects.filter(exam=exam, date=day, start_time=start).first()
    rooms = Room.objects.filter(school=request.school, is_active=True).order_by("name")
    here = reverse("examinations:seat_plan", args=[exam.pk]) + "?" + urlencode({"date": day, "time": f"{start:%H:%M}"})

    kind = request.GET.get("print")
    if kind in SEAT_PLAN_PRINTS:
        if plan is None or not plan.seats.exists():
            messages.error(request, "Seat the students before printing.")
            return redirect(here)
        return seat_plan_pdf(request.school, plan, kind)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "seat":
                raw = (request.POST.get("seats_per_room") or "").strip()
                if raw and (not raw.isdigit() or int(raw) < 1):
                    raise ValidationError("Seats per room must be a whole number, or left blank.")
                plan = build_plan(
                    user=request.user,
                    exam=exam,
                    date=day,
                    start_time=start,
                    rooms=list(rooms.filter(pk__in=request.POST.getlist("rooms"))),
                    mix_classes=request.POST.get("mix_classes") == "on",
                    seats_per_room=int(raw) if raw else None,
                )
                messages.success(request, f"Seated {plan.seats.count()} students.")
            elif action in ("lock", "unlock") and plan is not None:
                set_locked(user=request.user, plan=plan, locked=action == "lock")
                messages.success(request, "Seat plan locked." if action == "lock" else "Seat plan unlocked.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect(here)

    lists = room_lists(plan) if plan else []
    used = {room.pk for room, _seats in lists}
    return render(
        request,
        "examinations/seat_plan.html",
        {
            "exam": exam,
            "day": day,
            "start": start,
            "papers": papers,
            "students": len(candidates(exam, papers)),
            "plan": plan,
            "rooms": rooms,
            "chosen": used or {room.pk for room in rooms},
            "summary": [
                (room, len(seats), sorted({str(seat.enrollment.section.class_level) for seat, _p in seats}))
                for room, seats in lists
            ],
            "seated": sum(len(seats) for _room, seats in lists),
            "prints": SEAT_PLAN_PRINTS,
            "here": here,
            "page_title": f"Seat plan · {exam.name}",
        },
    )
