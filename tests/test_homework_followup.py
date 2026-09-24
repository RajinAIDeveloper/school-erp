"""
Phase 5c: planning and following homework up.

The class calendar shows each section's week against the school's limit. A class teacher sees
what each student has not handed in. Homework marks go into an exam part only when a teacher
exports them in the marks-import format. Guardians get one SMS a week about missing work, if
the school has the module and has switched the digest on, and only guardians who accept SMS.
Work checked in class that nobody has checked yet is never called missing.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from homework import services
from homework.models import DailyLimit, Submission, Task
from messaging.models import SMSMessage, SMSTemplate
from messaging.notifications import ensure_default_templates, notify_homework_digest


def login(user):
    client = Client()
    client.force_login(user)
    return client


def set_task(erp, *, due, published, title="Fractions", **extra):
    task = Task(
        school=erp.school,
        academic_year=erp.year,
        class_level=erp.level,
        subject=erp.subject,
        title=title,
        estimated_minutes=extra.pop("estimated_minutes", 20),
        **extra,
    )
    return services.save_task(user=erp.teacher, task=task, targets={erp.section: due}, action="publish", now=published)


def record(erp, task, status, **fields):
    row = task.submissions.get()
    services.check_rows(
        user=erp.teacher,
        task=task,
        target=task.targets.get(),
        return_work=True,
        now=timezone.now(),
        rows=[
            {
                "id": row.pk,
                "version": row.version,
                "status": status,
                "late": False,
                "mark": fields.get("mark"),
                "grade": "",
                "feedback": "",
                "reason": "",
            }
        ],
    )
    return task.submissions.get()


# ------------------------------------------------------------------ the weekly SMS


@pytest.fixture
def digest(erp, homework):
    erp.school.notify_homework_sms = True
    erp.school.save()
    erp.guardian.sms_opt_in = True
    erp.guardian.save()
    return erp


def test_one_message_a_week_for_each_child_with_missing_work(digest):
    erp = digest
    now = timezone.now()
    not_done = set_task(erp, due=now - timedelta(days=2), published=now - timedelta(days=4), title="Tables")
    record(erp, not_done, "not_done")
    set_task(
        erp,
        due=now - timedelta(days=1),
        published=now - timedelta(days=3),
        title="Photo of 4.3",
        hand_in=Task.HandIn.ONLINE,
    )
    # Checked in class, but nobody has checked it yet: not missing.
    set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=3), title="Unchecked")
    assert notify_homework_digest(erp.school, now) == 1
    message = SMSMessage.objects.get()
    assert "Ayesha" in message.body and "2 homework task(s)" in message.body and "Math" in message.body
    # Again the same week: nothing new. The next week it is sent again.
    assert notify_homework_digest(erp.school, now + timedelta(hours=2)) == 0
    record_week_later = now + timedelta(days=7)
    late = set_task(erp, due=record_week_later - timedelta(days=1), published=now, title="Week two")
    record(erp, late, "not_done")
    assert notify_homework_digest(erp.school, record_week_later) == 1
    assert SMSMessage.objects.count() == 2


def test_no_message_without_the_module_the_switch_or_the_guardians_consent(digest):
    erp = digest
    now = timezone.now()
    record(erp, set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=2)), "not_done")
    erp.guardian.sms_opt_in = False
    erp.guardian.save()
    assert notify_homework_digest(erp.school, now) == 0
    erp.guardian.sms_opt_in = True
    erp.guardian.save()
    erp.school.notify_homework_sms = False
    erp.school.save()
    assert notify_homework_digest(erp.school, now) == 0
    erp.school.notify_homework_sms = True
    erp.school.homework_enabled = False
    erp.school.save()
    assert notify_homework_digest(erp.school, now) == 0
    assert not SMSMessage.objects.exists()


def test_nothing_missing_sends_nothing(digest):
    erp = digest
    now = timezone.now()
    record(erp, set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=2)), "done")
    assert notify_homework_digest(erp.school, now) == 0


def test_the_digest_command_goes_only_to_schools_that_asked(digest):
    from io import StringIO

    from django.core.management import call_command

    erp = digest
    now = timezone.now()
    record(erp, set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=2)), "not_done")
    out = StringIO()
    call_command("homework_digest", "--dry-run", stdout=out)
    assert "would send" in out.getvalue() and not SMSMessage.objects.exists()
    call_command("homework_digest", stdout=out)
    assert SMSMessage.objects.count() == 1


def test_the_switch_and_its_message_belong_to_the_module(erp):
    ensure_default_templates(erp.school)
    assert not SMSTemplate.objects.filter(school=erp.school, name="Homework digest").exists()
    client = login(erp.admin)
    assert "Weekly homework digest" not in client.get("/settings/notifications/").content.decode()
    client.post("/settings/notifications/", {"notify_homework_sms": "on"})
    erp.school.refresh_from_db()
    assert not erp.school.notify_homework_sms
    erp.school.homework_enabled = True
    erp.school.save()
    assert "Weekly homework digest" in client.get("/settings/notifications/").content.decode()
    ensure_default_templates(erp.school)
    assert SMSTemplate.objects.filter(school=erp.school, name="Homework digest").exists()


# ------------------------------------------------------------------ the calendar and missing work


def test_the_class_calendar_shows_each_sections_week(erp, homework):
    DailyLimit.objects.create(school=erp.school, class_level=erp.level, minutes=30)
    now = timezone.now()
    day = timezone.localdate(now) + timedelta(days=1)
    while day.isoweekday() in erp.school.weekend_day_numbers:
        day += timedelta(days=1)
    due = timezone.make_aware(datetime.combine(day, time(10)))
    set_task(erp, due=due, published=now - timedelta(hours=1), title="Heavy", estimated_minutes=45)
    week = timezone.localdate(due).strftime("%Y-%m-%d")
    page = login(erp.teacher).get(f"/homework/calendar/?week={week}")
    body = page.content.decode()
    assert page.status_code == 200 and "45" in body and "bg-red-50" in body and erp.other_section.name in body
    assert login(erp.accountant).get("/homework/calendar/").status_code == 403


def test_the_class_teacher_sees_what_is_missing(erp, homework):
    now = timezone.now()
    record(
        erp, set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=3), title="Tables"), "not_done"
    )
    set_task(erp, due=now - timedelta(days=1), published=now - timedelta(days=3), title="Unchecked")
    # The subject teacher who is not the class teacher is refused.
    assert login(erp.teacher).get("/homework/missing/").status_code == 403
    erp.section.class_teacher = erp.employee
    erp.section.save()
    body = login(erp.teacher).get(f"/homework/missing/?section={erp.section.pk}").content.decode()
    assert "Tables" in body and "not done" in body and "Unchecked" not in body
    assert login(erp.admin).get("/homework/missing/").status_code == 200


# ------------------------------------------------------------------ marks into an exam part


def test_homework_marks_go_into_an_exam_part_on_purpose(erp, homework):
    from openpyxl import load_workbook

    from examinations.mark_import import check, read_rows

    now = timezone.now()
    first = set_task(
        erp,
        due=now - timedelta(days=2),
        published=now - timedelta(days=4),
        title="Quiz 1",
        marking=Task.Marking.MARKS,
        max_marks=Decimal(10),
    )
    second = set_task(
        erp,
        due=now - timedelta(days=1),
        published=now - timedelta(days=3),
        title="Quiz 2",
        marking=Task.Marking.MARKS,
        max_marks=Decimal(20),
    )
    record(erp, first, "done", mark=Decimal(8))  # 80%
    record(erp, second, "not_done")  # missing
    url = f"/homework/{first.pk}/export/{erp.section.pk}/"
    client = login(erp.teacher)
    form = client.get(url).content.decode()
    assert "Quiz 1" in form and "Quiz 2" in form and erp.exam.name in form
    blank = client.get(
        url, {"task": [first.pk, second.pk], "paper": erp.schedule.pk, "missing": "blank", "format": "xlsx"}
    )
    sheet = load_workbook(BytesIO(blank.content)).worksheets[0]
    assert [c.value for c in sheet[1]] == ["Roll", "Student ID", "Student", "Marks (100)"]
    assert sheet["D2"].value == "80"
    zero = client.get(
        url, {"task": [first.pk, second.pk], "paper": erp.schedule.pk, "missing": "zero", "format": "xlsx"}
    )
    upload = SimpleUploadedFile("marks.xlsx", zero.content)
    prepared, problems, _skipped = check(erp.schedule, [], [erp.enrollment], {}, read_rows(upload))
    assert not problems and Decimal(prepared[0]["score"]) == 40  # the average of 80% and 0%
    # A teacher of another section cannot take them.
    from tests.test_homework_setting import _colleague

    assert login(_colleague(erp)).get(url).status_code == 404


def test_excused_work_is_left_out_of_the_mark(erp, homework):
    from homework.followup import export_marks

    now = timezone.now()
    task = set_task(
        erp,
        due=now - timedelta(days=1),
        published=now - timedelta(days=2),
        marking=Task.Marking.MARKS,
        max_marks=Decimal(10),
    )
    services.excuse(user=erp.teacher, submission=task.submissions.get(), reason="Ill", now=now)
    marks = export_marks([task], erp.section, missing_as_zero=True, out_of=50, students=[erp.enrollment])
    assert marks[erp.enrollment.pk] is None


# ------------------------------------------------------------------ the dashboards


def test_the_teacher_and_the_managers_see_homework_on_their_dashboards(erp, homework):
    now = timezone.now()
    set_task(erp, due=now - timedelta(hours=2), published=now - timedelta(days=1), title="Check me")
    teacher = login(erp.teacher).get("/").content.decode()
    assert "Homework to check" in teacher and "Check me" in teacher
    record(erp, Task.objects.get(title="Check me"), "done")
    manager = login(erp.admin).get("/").content.decode()
    assert "Homework handed in over the last week" in manager and "100%" in manager
    erp.school.homework_enabled = False
    erp.school.save()
    assert "Homework to check" not in login(erp.teacher).get("/").content.decode()


def test_the_demo_school_has_homework(db, settings, tmp_path):
    from django.core.management import call_command

    from core.models import School

    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo", slug="homework-demo", demo_password="Seed-pass-2026", verbosity=0)
    school = School.objects.get(slug="homework-demo")
    assert school.homework_enabled
    assert Task.objects.filter(school=school).count() == 3
    assert Submission.objects.filter(school=school, returned_at__isnull=False).exists()
