"""
Repository hygiene and navigation-integrity tests.

These guard the things that rot quietly: templates left behind by a rewrite, header
links that lead to a 403, and management lists that leak record titles to roles which
only hold the read permission for their own portal screen.
"""

import re
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from academics.models import AcademicYear
from core.models import AuditLog
from users.models import User

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
# Django looks these up by convention, so no source file names them.
CONVENTIONAL = {"403.html", "404.html", "500.html"}


def source_files():
    for folder in (
        "core",
        "users",
        "academics",
        "students",
        "employees",
        "attendance",
        "fees",
        "finance",
        "examinations",
        "timetable",
        "downloads",
        "messaging",
        "holidays",
        "reports",
        "config",
        "templates",
    ):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in (".py", ".html", ".txt") and path.is_file():
                yield path


def test_every_template_on_disk_is_referenced():
    """A template nobody renders is dead weight that misleads the next reader."""
    haystack = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in source_files())
    orphans = []
    for path in TEMPLATES.rglob("*"):
        if not path.is_file() or path.suffix not in (".html", ".txt"):
            continue
        relative = path.relative_to(TEMPLATES).as_posix()
        if relative in CONVENTIONAL or relative.startswith("registration/"):
            continue
        if relative not in haystack:
            orphans.append(relative)
    assert not orphans, f"Templates on disk that nothing references: {orphans}"


def test_no_template_extends_a_missing_base():
    """Catches leftovers from an earlier layout, e.g. {% extends "base.html" %}."""
    for path in TEMPLATES.rglob("*.html"):
        for parent in re.findall(r'{%\s*extends\s+"([^"]+)"', path.read_text(encoding="utf-8")):
            assert (TEMPLATES / parent).exists(), f"{path.relative_to(ROOT)} extends missing {parent}"


LIST_SCREENS = ["/students/", "/employees/", "/fees/", "/exams/", "/holidays/", "/settings/year/"]


@pytest.mark.parametrize("role", ["Administrator", "Principal", "Accountant", "Teacher"])
def test_list_header_actions_are_permission_filtered(erp, role):
    """Every header link a role is shown must actually open for that role."""
    user = {"Administrator": erp.admin, "Accountant": erp.accountant, "Teacher": erp.teacher}.get(role)
    if user is None:
        user = User.objects.create_user("matrix-" + role, school=erp.school, password="Test-pass-9842")
        user.groups.add(Group.objects.get(name=role))
    client = Client()
    client.force_login(user)
    for screen in LIST_SCREENS:
        listing = client.get(screen)
        if listing.status_code != 200:
            continue
        for label, url in listing.context["extra_actions"]:
            assert client.get(url).status_code == 200, f"{role}: '{label}' ({url}) from {screen} is not openable"
        if listing.context["create_url"]:
            assert client.get(listing.context["create_url"]).status_code == 200


def test_student_cannot_list_staff_only_download_titles(erp, settings, tmp_path):
    """Students hold view_downloaditem for their own list; the management list is not theirs."""
    from downloads.models import DownloadCategory, DownloadItem

    settings.MEDIA_ROOT = tmp_path
    category = DownloadCategory.objects.create(school=erp.school, name="Internal")
    DownloadItem.objects.create(
        school=erp.school,
        category=category,
        title="Salary revision circular",
        audience="staff",
        file=SimpleUploadedFile("circular.txt", b"staff only"),
    )
    client = Client()
    client.force_login(erp.parent)
    assert client.get("/downloads/item/").status_code == 403
    assert b"Salary revision circular" not in client.get("/downloads/").content
    client.force_login(erp.admin)
    assert b"Salary revision circular" in client.get("/downloads/item/").content


def test_routine_slot_management_list_is_for_editors_only(erp):
    client = Client()
    client.force_login(erp.parent)
    assert client.get("/routine/slot/").status_code == 403
    assert client.get("/routine/").status_code == 200


def test_year_switch_sets_current_and_audits(erp):
    later = AcademicYear.objects.create(school=erp.school, name="2027", start_date="2027-01-01", end_date="2027-12-31")
    client = Client()
    client.force_login(erp.admin)
    assert client.get(f"/settings/year/{later.pk}/switch/").status_code == 405
    assert client.post(f"/settings/year/{later.pk}/switch/").status_code == 302
    later.refresh_from_db()
    erp.year.refresh_from_db()
    assert later.is_current and not erp.year.is_current
    assert AuditLog.objects.filter(action="academic_year.switched", school=erp.school).exists()
    client.force_login(erp.teacher)
    assert client.post(f"/settings/year/{erp.year.pk}/switch/").status_code == 403


def test_year_switch_cannot_touch_another_school(erp):
    foreign = AcademicYear.objects.create(school=erp.other, name="2027", start_date="2027-01-01", end_date="2027-12-31")
    client = Client()
    client.force_login(erp.admin)
    assert client.post(f"/settings/year/{foreign.pk}/switch/").status_code == 404


def test_portal_index_uses_real_separators(erp):
    client = Client()
    client.force_login(erp.parent)
    body = client.get("/portal/").content.decode()
    assert "Ayesha" in body
    assert "?" not in body.split("Ayesha")[1][:40], "portal card heading still contains mojibake"


def test_sections_json_is_school_scoped(erp):
    client = Client()
    client.force_login(erp.admin)
    payload = client.get(f"/academics/sections.json?class_level={erp.level.pk}").json()
    assert {row["id"] for row in payload} == {erp.section.pk, erp.other_section.pk}
    from academics.models import ClassLevel

    foreign = ClassLevel.objects.create(school=erp.other, name="Other class", order=1)
    assert client.get(f"/academics/sections.json?class_level={foreign.pk}").json() == []
    assert client.get("/academics/sections.json?class_level=not-a-number").json() == []


def test_the_public_surface_is_exactly_what_we_intend(erp):
    """
    Anything reachable without signing in is a decision, not an accident.

    A new view that forgets its permission decorator would silently join this list, so the
    list is asserted rather than described.
    """
    from django.urls import get_resolver

    from core.management.commands.role_matrix import SKIP_PREFIXES, view_permission, walk

    public = {
        f"/{url}"
        for url, _name, permission, _also in walk(get_resolver().url_patterns)
        if permission == "(public)" and not url.startswith(SKIP_PREFIXES)
    }
    assert public == {
        # Serves a file only when its audience is Public; anything else demands a sign-in.
        "/downloads/<int:pk>/file/",
        # Confirms a report card is genuine without naming a child or showing a mark.
        "/exams/verify/<uuid:code>/",
        # The same check for a combined result, such as an annual result.
        "/exams/combined/verify/<uuid:code>/",
        # Families look up a published result with the student ID and the admit card's result
        # code, when the school switches it on; rate limited, published results only.
        "/results/<slug:slug>/",
        # The payment gateway returns the payer and notifies the school here, without a session;
        # a payment is believed only after asking the gateway itself. The demonstration gateway's
        # page works only where the demonstration gateway is allowed.
        "/fees/online/notify/",
        "/fees/online/<str:tran_id>/<str:outcome>/",
        "/fees/online/<str:tran_id>/demo/",
        # Anyone holding a certificate may check it; the page shows initials, never the record.
        "/students/certificates/verify/<uuid:code>/",
        # A liveness probe; a load balancer has no session.
        "/healthz/",
    }, f"unexpected public endpoints: {public}"


def test_a_public_download_is_still_refused_when_the_audience_is_not_public(erp, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from downloads.models import DownloadCategory, DownloadItem

    settings.MEDIA_ROOT = tmp_path
    category = DownloadCategory.objects.create(school=erp.school, name="Internal")
    private = DownloadItem.objects.create(
        school=erp.school,
        category=category,
        title="Staff circular",
        audience="staff",
        file=SimpleUploadedFile("circular.txt", b"private"),
    )
    public = DownloadItem.objects.create(
        school=erp.school,
        category=category,
        title="Prospectus",
        audience="public",
        file=SimpleUploadedFile("prospectus.txt", b"anyone may read this"),
    )
    anonymous = Client()
    assert anonymous.get(f"/downloads/{private.pk}/file/").status_code == 403
    response = anonymous.get(f"/downloads/{public.pk}/file/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"anyone may read this"
