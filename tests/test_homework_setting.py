"""
Phase 5a: setting homework and checking it in class.

Work goes only to the students who take the subject, is due at the section's next lesson by
default, warns (never blocks) when a day is over the school's limit, and is checked on a grid
as fast as the register. A family can tick work done at home; the teacher's check stands.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.utils import timezone

from academics.models import Section, SubjectTeacher
from homework import services
from homework.access import may_set, teaching_pairs
from homework.models import DailyLimit, Submission, Task, TaskSection
from students.models import Enrollment, Student


def login(user):
    client = Client()
    client.force_login(user)
    return client


def at(day, hour=14, minute=0):
    return timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute)))


SUNDAY = date(2026, 9, 20)  # the weekend is Friday and Saturday


def new_task(erp, subject=None, **extra):
    fields = {"title": "Fractions, exercise 4.2", "estimated_minutes": 20, **extra}
    return Task(
        school=erp.school, academic_year=erp.year, class_level=erp.level, subject=subject or erp.subject, **fields
    )


def set_task(erp, *, due, now, action="publish", selected=None, **extra):
    return services.save_task(
        user=erp.teacher,
        task=new_task(erp, **extra),
        targets={erp.section: due},
        action=action,
        selected=selected,
        now=now,
    )


# ------------------------------------------------------------------ when it is due


def test_work_is_due_at_the_next_lesson(erp, homework):
    due, reason = services.suggest_due(erp.section, erp.subject, at(SUNDAY), erp.year)
    assert due == at(SUNDAY + timedelta(days=1), 10) and "Math" in reason  # Monday, 2nd period
    due, _ = services.suggest_due(erp.section, erp.subject, at(SUNDAY + timedelta(days=1)), erp.year)
    assert due == at(SUNDAY + timedelta(days=3), 9)  # Wednesday, 1st period
    # From Wednesday afternoon the next lesson is after the weekend.
    due, _ = services.suggest_due(erp.section, erp.subject, at(SUNDAY + timedelta(days=3)), erp.year)
    assert due == at(SUNDAY + timedelta(days=8), 10)


def test_a_holiday_is_skipped(erp, homework):
    from holidays.models import Holiday

    monday = SUNDAY + timedelta(days=1)
    Holiday.objects.create(school=erp.school, name="Holiday", start_date=monday, end_date=monday)
    due, _ = services.suggest_due(erp.section, erp.subject, at(SUNDAY), erp.year)
    assert due == at(SUNDAY + timedelta(days=3), 9)


def test_a_subject_off_the_routine_is_due_the_next_school_day(erp, homework):
    from academics.models import Subject

    english = Subject.objects.create(school=erp.school, name="English")
    thursday = SUNDAY + timedelta(days=4)
    due, reason = services.suggest_due(erp.section, english, at(thursday), erp.year)
    assert due == at(SUNDAY + timedelta(days=7), 9)  # Sunday, past the weekend, first period
    assert "not on the routine" in reason


def test_a_paper_follows_the_lessons_of_its_subject(erp, homework, board):
    from timetable.models import RoutineSlot

    b1 = board.schedules["101"].subject
    b2 = board.schedules["102"].subject
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=board.section,
        weekday=2,
        period=homework.periods[0],
        subject=b1,
    )
    tuesday = at(SUNDAY + timedelta(days=2), 9)
    for subject in (board.subjects.bangla, b1, b2):  # the whole subject, its own paper, the other paper
        due, _ = services.suggest_due(board.section, subject, at(SUNDAY), erp.year)
        assert due == tuesday, subject


# ------------------------------------------------------------------ who gets it


def test_only_the_students_who_take_the_subject_get_the_work(erp, board):
    def names(subject):
        return [e.student.first_name for e in services.eligible(erp.year, board.level, subject, [board.section])]

    s = board.schedules
    assert names(board.subjects.physics) == ["Nabila"]  # Science only
    assert names(s["112"].subject) == ["Rupa"]  # Hindu religion
    assert names(s["111"].subject) == ["Nabila"]  # Islam
    assert names(board.subjects.agriculture) == ["Rupa"]  # her 4th subject
    assert names(board.subjects.higher_math) == ["Nabila"]
    assert names(board.subjects.bangla) == ["Nabila", "Rupa"]  # the subject, through its papers


def test_a_teacher_of_the_papers_may_set_work_for_the_subject(erp, board):
    pairs = teaching_pairs(erp.teacher, erp.school, erp.year)
    assert (board.section.pk, board.subjects.bangla.pk) in pairs
    assert may_set(erp.teacher, erp.school, board.subjects.bangla, [board.section], erp.year)
    # A whole-subject teacher covers each paper.
    SubjectTeacher.objects.filter(section=board.section, subject__combines_into=board.subjects.bangla).delete()
    SubjectTeacher.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=board.section,
        subject=board.subjects.bangla,
        teacher=erp.employee,
    )
    assert may_set(erp.teacher, erp.school, board.schedules["102"].subject, [board.section], erp.year)


def test_nobody_sets_work_where_they_do_not_teach(erp, homework):
    other = Section.objects.create(school=erp.school, class_level=erp.level, name="C")
    with pytest.raises(PermissionDenied):
        services.save_task(
            user=erp.teacher, task=new_task(erp), targets={other: at(SUNDAY, 20)}, action="publish", now=at(SUNDAY)
        )
    # Nor in another school's section.
    from academics.models import ClassLevel

    level = ClassLevel.objects.create(school=erp.other, name="Class 1", order=1)
    theirs = Section.objects.create(school=erp.other, class_level=level, name="A")
    with pytest.raises(PermissionDenied):
        services.save_task(
            user=erp.admin, task=new_task(erp), targets={theirs: at(SUNDAY, 20)}, action="publish", now=at(SUNDAY)
        )


def test_publishing_gives_each_student_a_record_and_chosen_work_goes_to_the_chosen(erp, homework):
    task = set_task(erp, due=at(SUNDAY + timedelta(days=1), 10), now=at(SUNDAY))
    assert [row.enrollment for row in task.submissions.all()] == [erp.enrollment]
    other = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Karim",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    second = Enrollment.objects.create(
        school=erp.school,
        student=other,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    chosen = set_task(erp, due=at(SUNDAY + timedelta(days=1), 10), now=at(SUNDAY), selected=[second.pk])
    assert list(chosen.submissions.values_list("enrollment", flat=True)) == [second.pk]
    with pytest.raises(ValidationError):
        set_task(erp, due=at(SUNDAY + timedelta(days=1), 10), now=at(SUNDAY), selected=[])


def test_the_due_time_must_come_after_the_work_goes_out(erp, homework):
    with pytest.raises(ValidationError):
        set_task(erp, due=at(SUNDAY, 9), now=at(SUNDAY))


def test_drafts_and_scheduled_work_stay_hidden_from_families(erp, homework):
    now = timezone.now()
    draft = set_task(erp, due=now + timedelta(days=2), now=now, action="draft", title="A draft")
    services.save_task(
        user=erp.teacher,
        task=new_task(erp, title="Tomorrow's work"),
        targets={erp.section: now + timedelta(days=3)},
        action="schedule",
        publish_at=now + timedelta(days=1),
        now=now,
    )
    page = login(erp.parent).get("/homework/todo/").content.decode()
    assert "A draft" not in page and "Tomorrow's work" not in page
    row = draft.submissions.get()
    assert login(erp.parent).get(f"/homework/todo/{row.pk}/").status_code == 404
    # The draft is its author's: a colleague does not see it.
    assert Task.objects.get(pk=draft.pk).status == Task.Status.DRAFT


def test_a_new_student_gets_open_work_and_a_leaver_drops_off(erp, homework):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(days=2), now=now)
    other = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Karim",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    joined = Enrollment.objects.create(
        school=erp.school,
        student=other,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    services.sync_task(task, now)
    assert task.submissions.filter(enrollment=joined).exists()
    joined.status = Enrollment.Status.LEFT
    joined.save()
    services.sync_task(task, now)
    assert not task.submissions.filter(enrollment=joined).exists()
    # Once the work is due the list stands.
    joined.status = Enrollment.Status.ENROLLED
    joined.save()
    services.sync_task(task, now + timedelta(days=3))
    assert not task.submissions.filter(enrollment=joined).exists()


# ------------------------------------------------------------------ how much is due


def test_the_load_warns_but_never_blocks(erp, homework):
    DailyLimit.objects.create(school=erp.school, class_level=erp.level, minutes=30)
    now = timezone.now()
    due = (now + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    set_task(erp, due=due, now=now, estimated_minutes=20, title="Already set")
    load = services.load_with(erp.section, due, 15)
    today = next(day for day in load["days"] if day["this"])
    assert today["minutes"] == 35 and load["over"]
    # A draft and another section's work do not count.
    set_task(erp, due=due, now=now, action="draft", estimated_minutes=100, title="Draft")
    assert next(day for day in services.load_with(erp.section, due, 0)["days"] if day["this"])["minutes"] == 20
    # Setting it anyway works, with a warning.
    client = login(erp.teacher)
    form = {
        "title": "More fractions",
        "kind": "practice",
        "hand_in": "in_class",
        "estimated_minutes": "15",
        "marking": "none",
        "marks_visible": "on",
        f"section_{erp.section.pk}": "1",
        f"due_date_{erp.section.pk}": timezone.localtime(due).strftime("%Y-%m-%d"),
        f"due_time_{erp.section.pk}": "10:00",
        "action": "publish",
    }
    response = client.post(f"/homework/new/?unit={erp.level.pk}-{erp.subject.pk}", form, follow=True)
    body = response.content.decode()
    assert Task.objects.filter(title="More fractions", status=Task.Status.PUBLISHED).exists()
    assert "35 minutes of homework due" in body and "limit for the class is 30" in body


def test_the_form_suggests_the_next_lesson_and_shows_the_load(erp, homework):
    page = login(erp.teacher).get(f"/homework/new/?unit={erp.level.pk}-{erp.subject.pk}").content.decode()
    assert "the next Math lesson" in page and "When is it due?" in page
    choose = login(erp.teacher).get("/homework/new/").content.decode()
    assert "Math" in choose and f"unit={erp.level.pk}-{erp.subject.pk}" in choose


# ------------------------------------------------------------------ checking it


def grid_row(row, **changes):
    values = {
        "id": row.pk,
        "version": row.version,
        "status": row.status,
        "late": row.late,
        "mark": row.mark,
        "grade": row.grade,
        "feedback": row.feedback,
        "reason": row.reason,
    }
    values.update(changes)
    return values


def test_the_teacher_checks_work_on_the_grid(erp, homework):
    now = timezone.now()
    task = set_task(
        erp, due=now + timedelta(hours=1), now=now - timedelta(days=1), marking="marks", max_marks=Decimal(10)
    )
    target = task.targets.get()
    row = task.submissions.get()
    later = now + timedelta(hours=2)
    # A mark out of range, and nothing is saved.
    with pytest.raises(ValidationError):
        services.check_rows(
            user=erp.teacher,
            task=task,
            target=target,
            rows=[grid_row(row, mark=Decimal(11))],
            return_work=True,
            now=later,
        )
    # A mark on work not yet checked records it as done.
    services.check_rows(
        user=erp.teacher,
        task=task,
        target=target,
        rows=[grid_row(row, mark=Decimal("7.5"), feedback="Good working.")],
        return_work=True,
        now=later,
    )
    row.refresh_from_db()
    assert row.status == Submission.Status.DONE and row.mark == Decimal("7.5") and row.returned_at
    # A save from a page opened before that one is refused.
    stale = grid_row(row, version=row.version - 1, status=Submission.Status.NOT_DONE)
    with pytest.raises(ValidationError, match="Someone else changed"):
        services.check_rows(user=erp.teacher, task=task, target=target, rows=[stale], return_work=True, now=later)
    # The family sees the returned feedback and the mark.
    page = login(erp.parent).get(f"/homework/todo/{row.pk}/").content.decode()
    assert "Good working." in page and "7.5" in page


def test_a_hidden_mark_stays_hidden(erp, homework):
    now = timezone.now()
    task = set_task(
        erp,
        due=now + timedelta(hours=1),
        now=now - timedelta(days=1),
        marking="marks",
        max_marks=Decimal(10),
        marks_visible=False,
    )
    row = task.submissions.get()
    services.check_rows(
        user=erp.teacher,
        task=task,
        target=task.targets.get(),
        rows=[grid_row(row, mark=Decimal(6), feedback="Check question 3.")],
        return_work=True,
        now=now,
    )
    page = login(erp.parent).get(f"/homework/todo/{row.pk}/").content.decode()
    assert "Check question 3." in page and "/ 10" not in page


def test_the_grid_page_saves_in_one_go(erp, homework):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(hours=1), now=now - timedelta(days=1))
    row = task.submissions.get()
    url = f"/homework/{task.pk}/check/{erp.section.pk}/"
    client = login(erp.teacher)
    assert "All done" in client.get(url).content.decode()
    response = client.post(
        url,
        {
            "row": [row.pk],
            f"version_{row.pk}": row.version,
            f"status_{row.pk}": "not_done",
            f"feedback_{row.pk}": "Please bring it tomorrow.",
            "return_work": "1",
        },
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.status == Submission.Status.NOT_DONE and row.checked_by == erp.teacher
    # A teacher of another section may not check it; the class teacher may only read it.
    colleague = _colleague(erp)
    assert login(colleague).get(url).status_code == 404
    erp.section.class_teacher = colleague.employee_profile
    erp.section.save()
    page = login(colleague).get(url)
    assert page.status_code == 200 and "You can read this but not change it." in page.content.decode()
    assert login(colleague).post(url, {"row": [row.pk]}).status_code == 403


def _colleague(erp):
    from django.contrib.auth.models import Group

    from employees.models import Employee
    from users.models import User

    user = User.objects.create_user(username="colleague", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Teacher"))
    Employee.objects.create(
        school=erp.school,
        user=user,
        employee_id="T2",
        first_name="Colleague",
        gender="F",
        joining_date=date(2025, 1, 1),
        phone="01712345670",
    )
    SubjectTeacher.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        subject=erp.subject,
        teacher=user.employee_profile,
    )
    return user


# ------------------------------------------------------------------ families


def test_a_guardian_ticks_work_done_and_the_teachers_check_stands(erp, homework):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(days=1), now=now - timedelta(minutes=1))
    row = task.submissions.get()
    client = login(erp.parent)
    todo = client.get("/homework/todo/").content.decode()
    assert task.title in todo and "To do" in todo
    client.post(f"/homework/todo/{row.pk}/", {"action": "done"})
    row.refresh_from_db()
    assert row.status == Submission.Status.DONE and row.by_guardian and row.source == Submission.Source.FAMILY
    assert not row.late
    # The student ticking it themselves is not recorded as the guardian.
    client.post(f"/homework/todo/{row.pk}/", {"action": "undo"})
    login(homework.student_user).post(f"/homework/todo/{row.pk}/", {"action": "done"})
    row.refresh_from_db()
    assert row.status == Submission.Status.DONE and not row.by_guardian
    # The teacher finds it was not done: that stands, and the family cannot change it back.
    row = services.check_rows(
        user=erp.teacher,
        task=task,
        target=task.targets.get(),
        rows=[grid_row(row, status=Submission.Status.NOT_DONE)],
        return_work=False,
        now=now,
    )
    row = task.submissions.get()
    assert row.status == Submission.Status.NOT_DONE and row.source == Submission.Source.TEACHER
    client.post(f"/homework/todo/{row.pk}/", {"action": "done"})
    assert task.submissions.get().status == Submission.Status.NOT_DONE


def test_another_familys_child_is_not_theirs_to_open(erp, homework, board):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(days=1), now=now - timedelta(minutes=1))
    row = task.submissions.get()
    from students.models import Guardian, StudentGuardian
    from users.models import User

    stranger = User.objects.create_user(username="stranger", school=erp.school, password="Test-pass-9842")
    guardian = Guardian.objects.create(
        school=erp.school, full_name="Another parent", phone="01712340009", user=stranger
    )
    StudentGuardian.objects.create(student=board.science.student, guardian=guardian, relation="father")
    client = login(stranger)
    assert client.get(f"/homework/todo/{row.pk}/").status_code == 404
    assert client.post(f"/homework/todo/{row.pk}/", {"action": "done"}).status_code == 404
    assert client.get(f"/homework/todo/?student={erp.student.pk}").status_code == 404
    assert task.submissions.get().status == Submission.Status.PENDING


def test_the_portal_card_counts_work_due_and_overdue(erp, homework):
    now = timezone.now()
    set_task(erp, due=now - timedelta(hours=1), now=now - timedelta(days=2), title="Late one")
    card = login(erp.parent).get("/portal/").content.decode()
    assert "1 overdue" in card
    todo = login(erp.parent).get("/homework/todo/").content.decode()
    assert "Overdue" in todo and "Late one" in todo


# ------------------------------------------------------------------ changing it


def test_what_is_fixed_once_work_is_recorded(erp, homework):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(days=1), now=now - timedelta(minutes=1))
    row = task.submissions.get()
    services.family_mark_done(user=erp.parent, submission=row, done=True, now=now)
    task.marking, task.max_marks = Task.Marking.MARKS, Decimal(10)
    with pytest.raises(ValidationError, match="cannot change"):
        services.save_task(
            user=erp.teacher, task=task, targets={erp.section: task.targets.get().due_at}, action="save", now=now
        )
    task.refresh_from_db()
    with pytest.raises(ValidationError):
        services.delete_task(user=erp.teacher, task=task)
    services.withdraw(user=erp.teacher, task=task)
    assert "Fractions" not in login(erp.parent).get("/homework/todo/").content.decode()
    # A task nobody has answered can be deleted.
    untouched = set_task(erp, due=now + timedelta(days=1), now=now - timedelta(minutes=1), title="Untouched")
    services.delete_task(user=erp.teacher, task=untouched)
    assert not Task.objects.filter(pk=untouched.pk).exists()


def test_changing_the_due_time_moves_a_family_tick_to_late(erp, homework):
    now = timezone.now()
    task = set_task(erp, due=now + timedelta(days=1), now=now - timedelta(hours=2))
    row = services.family_mark_done(user=erp.parent, submission=task.submissions.get(), done=True, now=now)
    assert not row.late
    # The teacher meant it to be due half an hour ago: the tick is now after the due time.
    services.save_task(
        user=erp.teacher,
        task=Task.objects.get(pk=task.pk),
        targets={erp.section: now - timedelta(minutes=30)},
        action="save",
        now=now,
    )
    assert TaskSection.objects.get(task=task).due_at < now
    assert task.submissions.get().late


# ------------------------------------------------------------------ Bangla


def test_the_screens_read_in_bangla(erp, homework):
    now = timezone.now()
    set_task(erp, due=now + timedelta(days=1), now=now - timedelta(minutes=1))
    erp.teacher.language = "bn"
    erp.teacher.save()
    assert "বাড়ির কাজ" in login(erp.teacher).get("/homework/").content.decode()
    erp.parent.language = "bn"
    erp.parent.save()
    assert "বাড়ির কাজ" in login(erp.parent).get("/homework/todo/").content.decode()
