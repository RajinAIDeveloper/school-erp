from pathlib import Path

import pytest
from django.template.loader import get_template
from django.urls import URLResolver, get_resolver, reverse

STATIC_PAGES = [
    "/",
    "/login/",
    "/students/",
    "/students/new/",
    "/students/enrollment/",
    "/students/enrollment/new/",
    "/students/import/",
    "/students/promote/",
    "/employees/",
    "/employees/new/",
    "/attendance/",
    "/attendance/report/",
    "/attendance/staff/",
    "/attendance/staff/report/",
    "/attendance/leave/",
    "/attendance/leave/new/",
    "/fees/",
    "/fees/generate/",
    "/fees/category/",
    "/fees/category/new/",
    "/fees/structure/",
    "/fees/structure/new/",
    "/fees/concession/new/",
    "/fees/reports/",
    "/finance/",
    "/finance/journals/new/",
    "/finance/account/",
    "/finance/account/new/",
    "/finance/reports/",
    "/finance/payroll/",
    "/finance/payroll/new/",
    "/exams/",
    "/exams/manage/new/",
    "/exams/schedule/",
    "/exams/schedule/new/",
    "/exams/scale/",
    "/exams/scale/new/",
    "/exams/marks/",
    "/exams/results/",
    "/exams/unlocks/",
    "/routine/",
    "/routine/slot/",
    "/routine/slot/new/",
    "/routine/period/",
    "/routine/period/new/",
    "/routine/room/new/",
    "/downloads/",
    "/downloads/new/",
    "/downloads/category/",
    "/downloads/category/new/",
    "/downloads/item/",
    "/sms/",
    "/sms/compose/",
    "/sms/template/",
    "/holidays/",
    "/holidays/new/",
    "/holidays/calendar.ics",
    "/settings/",
    "/settings/sms/",
    "/settings/audit/",
    "/settings/year/",
    "/settings/year/new/",
    "/settings/term/new/",
    "/settings/class/new/",
    "/settings/section/new/",
    "/settings/subject/new/",
    "/settings/subject-teacher/new/",
    "/settings/department/new/",
    "/settings/designation/new/",
    "/settings/leave-type/new/",
    "/users/",
    "/users/new/",
    "/users/profile/",
    "/password/change/",
    "/password/reset/",
]


@pytest.mark.parametrize("url", STATIC_PAGES)
def test_screens_render(admin_client, url):
    response = admin_client.get(url)
    assert response.status_code == 200, (url, response.status_code)


def test_existing_object_pages(admin_client, erp, invoice):
    for url in [
        f"/students/{erp.student.pk}/",
        f"/students/{erp.student.pk}/edit/",
        f"/students/{erp.student.pk}/guardian/",
        f"/students/{erp.student.pk}/documents/new/",
        f"/students/{erp.student.pk}/id-card.pdf",
        f"/fees/{invoice.pk}/",
        f"/exams/{erp.exam.pk}/",
        f"/exams/scales/{erp.scale.pk}/rules/",
        f"/users/{erp.admin.pk}/edit/",
        f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}",
        f"/attendance/?date=2026-09-21&section={erp.section.pk}",
        f"/attendance/report/?date=2026-09-21&section={erp.section.pk}",
        f"/exams/results/?exam={erp.exam.pk}&class_level={erp.level.pk}",
    ]:
        r = admin_client.get(url)
        assert r.status_code == 200, (url, r.status_code)


def test_anonymous_is_redirected(client, erp):
    for url in ["/", "/students/", "/fees/", "/exams/", "/downloads/", "/users/", "/settings/"]:
        assert client.get(url).status_code == 302


def test_portal_parent(client, erp, invoice):
    client.force_login(erp.parent)
    assert client.get("/").status_code == 200
    assert client.get(f"/students/{erp.student.pk}/").status_code == 200
    assert client.get(f"/fees/{invoice.pk}/").status_code == 200
    assert client.get("/users/").status_code == 403
    assert client.get("/fees/").status_code == 403


def test_teacher_roster_scoped(client, erp):
    client.force_login(erp.teacher)
    r = client.get(f"/attendance/?date=2026-09-21&section={erp.other_section.pk}")
    assert r.status_code == 200
    assert b"Select a valid choice" in r.content


def test_no_school_access(client, erp):
    from users.models import User

    user = User.objects.create_user("unassigned", password="Test-pass-9842")
    client.force_login(user)
    assert client.get("/").status_code == 200
    assert client.get("/students/").status_code == 403
