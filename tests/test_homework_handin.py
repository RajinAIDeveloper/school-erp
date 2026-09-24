"""
Phase 5b: handing homework in online, and the teacher's answer.

A file is judged by what is in it, not its name; photos are redrawn smaller and lose their
EXIF data; nothing is stored under a child's name; and each file is shown only to the people
who may see that child's work. The teacher returns work, asks for it again, gives more time
or excuses a student. Old files go when the school's keeping period ends, and erasing a
former student takes their work with them.
"""

import re
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from homework import services
from homework.files import prepare
from homework.models import Submission, SubmissionFile, Task
from homework.privacy import erase_homework, purge_files


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def login(user):
    client = Client()
    client.force_login(user)
    return client


def photo(name="page.jpg", size=(3000, 4000), fmt="JPEG", maker="PhoneCam"):
    image = Image.new("RGB", size, "white")
    exif = Image.Exif()
    exif[0x010F] = maker  # the camera's make, standing in for everything EXIF carries
    out = BytesIO()
    image.save(out, fmt, exif=exif) if fmt == "JPEG" else image.save(out, fmt)
    return SimpleUploadedFile(name, out.getvalue(), content_type="image/jpeg")


def pdf(name="work.pdf", size=100):
    return SimpleUploadedFile(name, b"%PDF-1.4\n" + b"0" * size, content_type="application/pdf")


def docx(name="essay.docx", macros=False):
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        if macros:
            archive.writestr("word/vbaProject.bin", "evil")
    return SimpleUploadedFile(name, out.getvalue())


def online_task(erp, now, *, due=None, allow_late=True, **extra):
    fields = {"title": "Photograph your fractions exercise", **extra}
    task = Task(
        school=erp.school,
        academic_year=erp.year,
        class_level=erp.level,
        subject=erp.subject,
        hand_in=Task.HandIn.ONLINE,
        allow_late=allow_late,
        **fields,
    )
    return services.save_task(
        user=erp.teacher,
        task=task,
        targets={erp.section: due or now + timedelta(days=1)},
        action="publish",
        now=now - timedelta(hours=1),
    )


# ------------------------------------------------------------------ what is accepted


def test_a_file_is_judged_by_what_is_in_it():
    with pytest.raises(ValidationError, match="not a PDF"):
        prepare(SimpleUploadedFile("page.jpg", b"<html><script>alert(1)</script></html>"))
    with pytest.raises(ValidationError, match="not a PDF"):
        prepare(SimpleUploadedFile("drawing.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"))
    zipped = BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(ValidationError, match="not a PDF"):
        prepare(SimpleUploadedFile("essay.docx", zipped.getvalue()))
    with pytest.raises(ValidationError, match="not a PDF"):
        prepare(docx(macros=True))
    assert prepare(pdf())[1] == "pdf"
    assert prepare(docx())[1] == "docx"
    with pytest.raises(ValidationError, match="10 MB"):
        prepare(pdf(size=10 * 1024 * 1024))


def test_a_photo_is_redrawn_smaller_without_its_exif():
    content, kind = prepare(photo())
    assert kind == "jpeg"
    with Image.open(BytesIO(content.read())) as image:
        assert max(image.size) == 2000
        assert "PhoneCam" not in str(dict(image.getexif()))
    # A PNG with transparency comes out as a JPEG on white.
    content, kind = prepare(photo("scan.png", size=(800, 600), fmt="PNG"))
    assert kind == "jpeg" and content.read()[:3] == b"\xff\xd8\xff"


# ------------------------------------------------------------------ handing in


def test_the_family_hands_in_photos_and_the_teacher_sees_them(erp, homework):
    now = timezone.now()
    task = online_task(erp, now)
    row = task.submissions.get()
    client = login(erp.parent)
    response = client.post(
        f"/homework/todo/{row.pk}/",
        {"action": "hand_in", "note": "Question 4 was hard.", "pages": [photo("Ayesha Rahman.jpg"), pdf()]},
    )
    assert response.status_code == 302
    row.refresh_from_db()
    assert row.status == Submission.Status.DONE and row.source == Submission.Source.ONLINE and row.by_guardian
    assert not row.late and row.note == "Question 4 was hard."
    pages = list(row.files.order_by("page"))
    assert [page.page for page in pages] == [1, 2] and [page.kind for page in pages] == ["jpeg", "pdf"]
    # Stored under a random name, by school and year: never the child's name.
    for page in pages:
        assert re.fullmatch(rf"homework/{erp.school.pk}/{erp.year.pk}/[0-9a-f]{{32}}\.(jpg|pdf)", page.file.name)
        assert "Ayesha" not in page.file.name
    review = login(erp.teacher).get(f"/homework/submission/{row.pk}/").content.decode()
    assert "Question 4 was hard." in review and f"/homework/file/{pages[0].pk}/" in review
    grid = login(erp.teacher).get(f"/homework/{task.pk}/check/{erp.section.pk}/").content.decode()
    assert "Handed in online by a guardian" in grid and "2 page(s)" in grid


def test_the_page_answers_the_phone_with_json(erp, homework):
    now = timezone.now()
    row = online_task(erp, now).submissions.get()
    client = login(homework.student_user)
    bad = client.post(
        f"/homework/todo/{row.pk}/",
        {"action": "hand_in", "pages": [SimpleUploadedFile("x.jpg", b"not a picture")]},
        HTTP_X_REQUESTED_WITH="fetch",
    )
    assert bad.status_code == 400 and "not a PDF" in bad.json()["errors"][0]
    good = client.post(
        f"/homework/todo/{row.pk}/", {"action": "hand_in", "pages": [photo()]}, HTTP_X_REQUESTED_WITH="fetch"
    )
    assert good.json()["ok"]
    row.refresh_from_db()
    assert not row.by_guardian  # the student handed it in themself


def test_limits_on_a_hand_in(erp, homework, settings):
    now = timezone.now()
    row = online_task(erp, now).submissions.get()
    with pytest.raises(ValidationError, match="at most 10"):
        services.hand_in_online(user=erp.parent, submission=row, uploads=[pdf() for _ in range(11)], now=now)
    with pytest.raises(ValidationError, match="Add a photo"):
        services.hand_in_online(user=erp.parent, submission=row, uploads=[], now=now)
    # A request over the site's upload ceiling stores nothing.
    settings.MAX_UPLOAD_REQUEST = 2000
    response = login(erp.parent).post(
        f"/homework/todo/{row.pk}/", {"action": "hand_in", "pages": [pdf(size=5000)]}, follow=True
    )
    assert "upload limit" in response.content.decode()
    assert not SubmissionFile.objects.exists()


def test_late_work_and_more_time(erp, homework):
    now = timezone.now()
    task = online_task(erp, now, due=now - timedelta(minutes=10))
    row = task.submissions.get()
    services.hand_in_online(user=erp.parent, submission=row, uploads=[pdf()], now=now)
    row.refresh_from_db()
    assert row.late
    strict = online_task(erp, now, due=now - timedelta(minutes=10), allow_late=False, title="Strict")
    strict_row = strict.submissions.get()
    with pytest.raises(ValidationError, match="not taken late"):
        services.hand_in_online(user=erp.parent, submission=strict_row, uploads=[pdf()], now=now)
    services.give_more_time(user=erp.teacher, submission=strict_row, extend_to=now + timedelta(days=1), now=now)
    services.hand_in_online(user=erp.parent, submission=strict_row, uploads=[pdf()], now=now)
    strict_row.refresh_from_db()
    assert strict_row.status == Submission.Status.DONE and not strict_row.late


def test_returned_work_is_closed_until_the_teacher_asks_for_it_again(erp, homework, django_capture_on_commit_callbacks):
    now = timezone.now()
    task = online_task(erp, now, marking="marks", max_marks=Decimal(10))
    row = task.submissions.get()
    services.hand_in_online(user=erp.parent, submission=row, uploads=[photo(), photo()], now=now)
    first = list(row.files.all())
    # A page can be taken out before the work is returned, and its file goes with it.
    with django_capture_on_commit_callbacks(execute=True):
        services.remove_page(user=erp.parent, page=first[1], now=now)
    assert not first[1].file.storage.exists(first[1].file.name)
    row.refresh_from_db()
    services.check_rows(
        user=erp.teacher,
        task=task,
        target=task.targets.get(),
        return_work=True,
        now=now,
        rows=[
            {
                "id": row.pk,
                "version": row.version,
                "status": "done",
                "late": False,
                "mark": Decimal(4),
                "grade": "",
                "feedback": "",
                "reason": "",
            }
        ],
    )
    row.refresh_from_db()
    with pytest.raises(ValidationError, match="returned"):
        services.hand_in_online(user=erp.parent, submission=row, uploads=[pdf()], now=now)
    services.ask_to_redo(user=erp.teacher, submission=row, feedback="Show the working for question 3.", now=now)
    page = login(erp.parent).get(f"/homework/todo/{row.pk}/").content.decode()
    assert "asked for this work again" in page and "Show the working for question 3." in page
    later = now + timedelta(minutes=5)
    services.hand_in_online(user=erp.parent, submission=row, uploads=[pdf()], now=later)
    row.refresh_from_db()
    assert row.attempt == 2 and row.status == Submission.Status.DONE and not row.redo_requested
    assert sorted(row.files.values_list("attempt", flat=True)) == [1, 2]  # the first attempt is kept


def test_the_teacher_excuses_with_a_reason(erp, homework):
    now = timezone.now()
    row = online_task(erp, now).submissions.get()
    with pytest.raises(ValidationError):
        services.excuse(user=erp.teacher, submission=row, reason="", now=now)
    client = login(erp.teacher)
    client.post(f"/homework/submission/{row.pk}/", {"action": "excuse", "reason": "In hospital this week"})
    row.refresh_from_db()
    assert row.status == Submission.Status.EXCUSED and row.reason == "In hospital this week"
    with pytest.raises(ValidationError, match="excused"):
        services.hand_in_online(user=erp.parent, submission=row, uploads=[pdf()], now=now)


# ------------------------------------------------------------------ who may open a file


def test_each_file_is_shown_only_to_those_who_may_see_the_childs_work(erp, homework, board):
    from tests.test_analytics_foundation import add_user
    from tests.test_homework_setting import _colleague

    now = timezone.now()
    row = online_task(erp, now).submissions.get()
    services.hand_in_online(user=erp.parent, submission=row, uploads=[photo()], now=now)
    url = f"/homework/file/{row.files.get().pk}/"
    for user in (erp.parent, homework.student_user, erp.teacher, erp.admin, add_user(erp, "vice", "Vice Principal")):
        response = login(user).get(url)
        assert response.status_code == 200, user
        assert response["X-Content-Type-Options"] == "nosniff" and "sandbox" in response["Content-Security-Policy"]
        assert response["Cache-Control"] == "private, no-store"
    colleague = _colleague(erp)  # teaches Math in the other section
    assert login(colleague).get(url).status_code == 404
    erp.section.class_teacher = colleague.employee_profile
    erp.section.save()
    assert login(colleague).get(url).status_code == 200  # now the class teacher
    for user in (erp.staff, erp.accountant):
        assert login(user).get(url).status_code == 404, user
    from students.models import Guardian, StudentGuardian
    from users.models import User

    stranger = User.objects.create_user(username="stranger", school=erp.school, password="Test-pass-9842")
    guardian = Guardian.objects.create(
        school=erp.school, full_name="Another parent", phone="01712340009", user=stranger
    )
    StudentGuardian.objects.create(student=board.science.student, guardian=guardian, relation="father")
    assert login(stranger).get(url).status_code == 404
    theirs = add_user(erp, "their_admin", "Administrator", school=erp.other)
    assert login(theirs).get(url).status_code == 404


def test_worksheets_go_to_the_families_the_task_is_for(erp, homework):
    client = login(erp.teacher)
    due = timezone.localtime(timezone.now() + timedelta(days=2))
    response = client.post(
        f"/homework/new/?unit={erp.level.pk}-{erp.subject.pk}",
        {
            "title": "Worksheet 7",
            "kind": "practice",
            "hand_in": "in_class",
            "estimated_minutes": "20",
            "marking": "none",
            f"section_{erp.section.pk}": "1",
            f"due_date_{erp.section.pk}": due.strftime("%Y-%m-%d"),
            f"due_time_{erp.section.pk}": "10:00",
            "resource_files": [pdf("Fractions worksheet.pdf")],
            "resource_link": "khanacademy.org/math/fractions",
            "resource_title": "Video on fractions",
            "action": "publish",
        },
    )
    assert response.status_code == 302
    task = Task.objects.get(title="Worksheet 7")
    sheet, link = task.resources.order_by("pk")
    assert sheet.title == "Fractions worksheet" and link.url == "https://khanacademy.org/math/fractions"
    page = login(erp.parent).get(f"/homework/todo/{task.submissions.get().pk}/").content.decode()
    assert "Fractions worksheet" in page and "Video on fractions" in page
    assert login(erp.parent).get(f"/homework/resource/{sheet.pk}/").status_code == 200
    assert login(erp.accountant).get(f"/homework/resource/{sheet.pk}/").status_code == 404


# ------------------------------------------------------------------ keeping and erasing


def _handed_in(erp, now):
    row = online_task(erp, now).submissions.get()
    services.hand_in_online(user=erp.parent, submission=row, uploads=[photo()], note="Done it", now=now)
    return row


def test_old_files_go_when_the_keeping_period_ends(erp, homework, django_capture_on_commit_callbacks):
    now = timezone.now()
    row = _handed_in(erp, now)
    stored = row.files.get()
    after_year = date(erp.year.end_date.year + 1, 1, 15)
    assert purge_files(erp.school, after_year) == 0  # six months have not passed
    later = date(erp.year.end_date.year + 1, 7, 15)
    assert purge_files(erp.school, later, dry_run=True) == 1 and SubmissionFile.objects.exists()
    # Even a school whose module was switched off has its old files removed.
    erp.school.homework_enabled = False
    erp.school.save()
    with django_capture_on_commit_callbacks(execute=True):
        assert purge_files(erp.school, later) == 1
    assert not SubmissionFile.objects.exists() and not stored.file.storage.exists(stored.file.name)
    row.refresh_from_db()
    assert row.status == Submission.Status.DONE  # the record of the work stays


def test_the_keeping_period_is_the_managers_to_set(erp, homework):
    client = login(erp.admin)
    client.post("/homework/limits/", {"files_months": "12"})
    erp.school.refresh_from_db()
    assert erp.school.homework_files_months == 12
    assert (
        "Keep handed-in files for 1 to 60 months."
        in client.post("/homework/limits/", {"files_months": "0"}, follow=True).content.decode()
    )


def test_erasing_a_former_student_takes_their_homework(erp, homework, django_capture_on_commit_callbacks):
    now = timezone.now()
    row = _handed_in(erp, now)
    Submission.objects.filter(pk=row.pk).update(feedback="Ayesha, well done.")
    stored = row.files.get()
    with django_capture_on_commit_callbacks(execute=True):
        erase_homework(erp.student)
    row.refresh_from_db()
    assert not row.files.exists() and not stored.file.storage.exists(stored.file.name)
    assert row.note == "" and row.feedback == ""
    # The erasure screen does it as part of erasing the student.
    import inspect

    from students import privacy

    assert "erase_homework" in inspect.getsource(privacy.erase_student)


# ------------------------------------------------------------------ in a real browser


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_a_large_photo_is_made_small_on_the_phone(live_server, erp, homework, settings, tmp_path):
    from pathlib import Path

    playwright = pytest.importorskip("playwright.sync_api")
    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
    now = timezone.now()
    row = online_task(erp, now).submissions.get()
    big = tmp_path / "page.png"
    Image.effect_noise((4000, 3000), 64).convert("RGB").save(big)
    root = Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - the browser binary is optional
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route(
            "**/static/**",
            lambda route: route.fulfill(path=str(root / "static" / route.request.url.split("/static/", 1)[1])),
        )
        page.goto(live_server.url + "/login/")
        page.locator('input[name="username"]').fill("parent")
        page.locator('input[name="password"]').fill("Test-pass-9842")
        page.get_by_role("button", name="Sign in").click()
        page.goto(f"{live_server.url}/homework/todo/{row.pk}/")
        page.locator("input[type=file][multiple]").set_input_files(str(big))
        page.wait_for_selector("[data-thumbs] img")
        with page.expect_navigation():
            page.get_by_role("button", name="Hand it in").click()
        browser.close()
    stored = SubmissionFile.objects.get()
    # The phone sent it at 1600 pixels at most (the server alone would keep 2000), a fraction of the original.
    with Image.open(stored.file.path) as image:
        assert max(image.size) == 1600
    assert stored.kind == "jpeg" and stored.size * 5 < big.stat().st_size
