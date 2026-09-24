"""
Exam officer extras: timetable clashes judged by who actually sits both papers, and official
results kept from families until the awarding body's release date.
"""

from datetime import date, time, timedelta

from django.test import Client
from django.utils import timezone

from examinations.clashes import timetable_clashes
from examinations.forms import ExamScheduleForm


def at(schedule, day, start, end):
    schedule.date, schedule.start_time, schedule.end_time = day, time(start), time(end)
    schedule.save()


def form_for(erp, schedule, **changes):
    data = {
        "exam": schedule.exam_id,
        "class_level": schedule.class_level_id,
        "subject": schedule.subject_id,
        "date": "2026-11-02",
        "start_time": "10:00",
        "end_time": "12:00",
        "full_marks": "100",
        "pass_marks": "33",
    }
    data.update(changes)
    return ExamScheduleForm(data=data, instance=schedule, school=erp.school)


def test_papers_no_student_sits_together_may_share_a_slot(erp, board):
    s, day = board.schedules, date(2026, 11, 2)
    at(s["136"], day, 10, 12)  # Physics: Science only
    at(s["110"], day, 10, 12)  # Geography: Humanities only
    assert timetable_clashes(board.exam) == []
    assert form_for(erp, s["110"]).is_valid()


def test_papers_a_student_sits_together_cannot_overlap(erp, board):
    s, day = board.schedules, date(2026, 11, 2)
    at(s["109"], day, 10, 12)  # General Math: everyone
    form = form_for(erp, s["101"], start_time="11:00", end_time="13:00")
    assert not form.is_valid()
    assert "2 student(s) sit both" in " ".join(form.errors["start_time"])
    # Back to back is fine.
    assert form_for(erp, s["101"], start_time="12:00", end_time="14:00").is_valid()


def test_the_exam_page_lists_clashes_already_in_the_timetable(erp, board):
    s, day = board.schedules, date(2026, 11, 2)
    at(s["109"], day, 10, 12)
    at(s["101"], day, 11, 13)
    clashes = timetable_clashes(board.exam)
    assert len(clashes) == 1 and "Nabila" in clashes[0]
    client = Client()
    client.force_login(erp.admin)
    assert "Timetable clashes" in client.get(f"/exams/{board.exam.pk}/").content.decode()


def test_official_results_wait_for_the_release_date(erp):
    from examinations.models import ExamSeries, OfficialResult, SeriesCandidate
    from examinations.official import official_results_for

    series = ExamSeries.objects.create(
        school=erp.school, body="cambridge", name="June 2027", results_date=timezone.localdate() + timedelta(days=5)
    )
    candidate = SeriesCandidate.objects.create(
        school=erp.school, series=series, student=erp.student, candidate_number="0001"
    )
    OfficialResult.objects.create(
        school=erp.school,
        candidate=candidate,
        syllabus_code="0580",
        grade="A",
        source="Statement of results",
        received_on=timezone.localdate(),
        checked_by=erp.admin,
        checked_at=timezone.now(),
    )
    assert official_results_for(erp.student, confirmed_only=True) == []
    assert len(official_results_for(erp.student, confirmed_only=False)) == 1
    series.results_date = timezone.localdate()
    series.save()
    assert len(official_results_for(erp.student, confirmed_only=True)) == 1
