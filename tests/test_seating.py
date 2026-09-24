"""
Seat plans: each sitting seats only the students who sit one of its papers, classes can take
turns so neighbours write different papers, rooms fill to capacity, a printed plan is locked,
and the door list, stickers and attendance sheet all come from the same stored seats.
"""

from datetime import date, time

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from core.pdf import styles
from examinations.documents import seat_plan_flowables
from examinations.models import ExamSchedule, Seat
from examinations.seating import build_plan, candidates, set_locked, sittings
from students.models import Enrollment, Student
from tests.test_rulebooks import card_texts
from timetable.models import Room

DAY = date(2026, 11, 3)
TEN = time(10, 0)


def login(user):
    client = Client()
    client.force_login(user)
    return client


def at(schedule, day=DAY, start=TEN):
    schedule.date, schedule.start_time = day, start
    schedule.save()
    return schedule


@pytest.fixture
def hall(erp, board):
    """
    Class 9 General Math and Class 1 Math at the same time: two classes in one sitting.
    Class 1 has Ayesha and Babul; Class 9 has Nabila and Rupa. Two rooms of two seats each.
    """
    at(board.schedules["109"])
    class_one_math = at(
        ExamSchedule.objects.create(
            school=erp.school,
            exam=board.exam,
            class_level=erp.level,
            subject=erp.subject,
            full_marks=100,
            pass_marks=33,
        )
    )
    babul = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Babul",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=babul,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    rooms = [Room.objects.create(school=erp.school, name=name, capacity=2) for name in ("101", "102")]
    return erp, board, class_one_math, rooms


def names(plan):
    return [(s.room.name, s.number, s.enrollment.student.first_name) for s in Seat.objects.filter(plan=plan)]


def test_a_sitting_seats_only_the_students_who_sit_its_papers(erp, board):
    at(board.schedules["136"])  # Physics: Science only
    at(board.schedules["110"])  # Geography: Humanities only
    at(board.schedules["126"], day=date(2026, 11, 4))  # Higher Math: Nabila's 4th subject
    first, second = sittings(board.exam)
    assert (first[0], first[1]) == (DAY, TEN) and len(first[2]) == 2
    who = {e.student.first_name: [p.subject.code for p in papers] for e, papers in candidates(board.exam, first[2])}
    assert who == {"Nabila": ["136"], "Rupa": ["110"]}
    assert [e.student.first_name for e, _p in candidates(board.exam, second[2])] == ["Nabila"]


def test_classes_take_turns_and_rooms_fill_to_capacity(hall):
    erp, board, _math, rooms = hall
    plan = build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms, mix_classes=True)
    assert names(plan) == [("101", 1, "Ayesha"), ("101", 2, "Nabila"), ("102", 1, "Babul"), ("102", 2, "Rupa")]
    plan = build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms, mix_classes=False)
    assert names(plan) == [("101", 1, "Ayesha"), ("101", 2, "Babul"), ("102", 1, "Nabila"), ("102", 2, "Rupa")]


def test_too_few_seats_is_refused_and_nothing_is_half_seated(hall):
    erp, board, _math, rooms = hall
    with pytest.raises(ValidationError, match="4 students sit at this time, but the rooms chosen have 2 seats"):
        build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms[:1])
    with pytest.raises(ValidationError, match="have 2 seats"):
        build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms, seats_per_room=1)
    assert not Seat.objects.exists()


def test_a_locked_plan_keeps_its_seats_until_unlocked(hall):
    erp, board, _math, rooms = hall
    plan = build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms)
    set_locked(user=erp.admin, plan=plan, locked=True)
    with pytest.raises(ValidationError, match="locked"):
        build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms, mix_classes=False)
    set_locked(user=erp.admin, plan=plan, locked=False)
    build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms, mix_classes=False)


def test_the_printouts_come_from_the_stored_seats(hall):
    erp, board, _math, rooms = hall
    plan = build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=rooms)
    door = card_texts(seat_plan_flowables(erp.school, plan, "door", styles()))
    assert "Nabila" in door and "Class / section" in door
    stickers = " ".join(card_texts(seat_plan_flowables(erp.school, plan, "stickers", styles())))
    assert "101 · Seat 1" in stickers and "Ayesha" in stickers and "Roll 1" in stickers
    sheet = card_texts(seat_plan_flowables(erp.school, plan, "attendance", styles()))
    assert "Script no." in sheet and "Signature" in sheet


def test_the_seat_plan_screens_seat_print_and_lock(hall):
    erp, board, _math, rooms = hall
    client = login(erp.admin)
    listing = client.get(f"/exams/{board.exam.pk}/seating/").content.decode()
    assert "03 Nov 2026" in listing and "Not seated" in listing
    url = f"/exams/{board.exam.pk}/seating/sitting/?date=2026-11-03&time=10:00"
    form = {"date": "2026-11-03", "time": "10:00", "rooms": [r.pk for r in rooms], "mix_classes": "on"}
    assert client.post(url, {**form, "action": "seat"}).status_code == 302
    assert Seat.objects.count() == 4
    for kind in ("door", "stickers", "attendance"):
        response = client.get(url + f"&print={kind}")
        assert response.status_code == 200 and response.content.startswith(b"%PDF"), kind
    client.post(url, {**form, "action": "lock"})
    assert "locked" in client.get(url).content.decode()


def test_only_managers_seat_and_only_this_schools_rooms(hall):
    erp, board, _math, rooms = hall
    assert login(erp.teacher).get(f"/exams/{board.exam.pk}/seating/").status_code == 403
    elsewhere = Room.objects.create(school=erp.other, name="Hall", capacity=50)
    with pytest.raises(PermissionDenied):
        build_plan(user=erp.admin, exam=board.exam, date=DAY, start_time=TEN, rooms=[elsewhere])
    assert not Seat.objects.exists()
