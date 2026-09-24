"""
Phase 4b: online admissions is a module only the platform administrator gives a school.

A school without it never meets it: every office address answers "not found" for every role,
and no page mentions admissions. With it, each role reaches only its own part.
"""

import pytest
from django.contrib.auth.models import Group
from django.urls import URLPattern, reverse

from admissions import urls as admission_urls
from tests.admissions_support import login
from users.models import User


def everyone(erp):
    def add(username, role):
        user = User.objects.create_user(username=username, school=erp.school, password="Test-pass-9842")
        user.groups.add(Group.objects.get(name=role))
        return user

    return {
        "Administrator": erp.admin,
        "Principal": add("principal", "Principal"),
        "Vice Principal": add("vice", "Vice Principal"),
        "Accountant": erp.accountant,
        "Teacher": erp.teacher,
        "Staff": erp.staff,
        "Student": add("pupil", "Student"),
        "Guardian": erp.parent,
    }


def office_addresses():
    for pattern in admission_urls.urlpatterns:
        assert isinstance(pattern, URLPattern)
        kwargs = {name: 1 for name in pattern.pattern.converters}
        yield reverse(f"admissions:{pattern.name}", kwargs=kwargs)


def test_every_office_address_is_not_found_for_everyone_without_the_module(erp):
    assert not erp.school.admissions_enabled
    for role, user in everyone(erp).items():
        client = login(user)
        for url in office_addresses():
            assert client.get(url).status_code == 404, (role, url)
            assert client.post(url).status_code in (404, 405), (role, url)


def test_signed_out_visitors_are_sent_to_sign_in(erp):
    from django.test import Client

    for url in office_addresses():
        response = Client().get(url)
        assert response.status_code == 302 and "/login/" in response["Location"], url


@pytest.mark.parametrize("page", ["/", "/settings/", "/portal/"])
def test_no_page_mentions_admissions_without_the_module(erp, page):
    for user in everyone(erp).values():
        body = login(user).get(page, follow=True).content.decode()
        assert "/admissions/" not in body and "Admissions" not in body, (user, page)


def test_with_the_module_each_role_reaches_only_its_own_part(erp, admissions):
    people = everyone(erp)
    row, admission_round = admissions.row, admissions.round
    for role in ("Administrator", "Principal", "Vice Principal", "Staff", "Accountant"):
        assert login(people[role]).get("/admissions/").status_code == 200, role
    for role in ("Teacher", "Student", "Guardian"):
        assert login(people[role]).get("/admissions/").status_code == 403, role
    # Setting up rounds is the managers'.
    for role in ("Staff", "Accountant"):
        assert login(people[role]).get("/admissions/rounds/new/").status_code == 403, role
        assert login(people[role]).get("/admissions/settings/").status_code == 403, role
    assert login(people["Vice Principal"]).get(f"/admissions/rounds/{admission_round.pk}/edit/").status_code == 200
    # Walk-in applications are the front office's; the accountant takes the fee.
    assert login(people["Staff"]).get(f"/admissions/classes/{row.pk}/apply/").status_code == 200
    assert login(people["Accountant"]).get(f"/admissions/classes/{row.pk}/apply/").status_code == 403


def test_with_the_module_it_appears_where_it_belongs(erp, admissions):
    assert 'href="/admissions/"' in login(erp.staff).get("/").content.decode()
    assert 'href="/admissions/"' not in login(erp.teacher).get("/").content.decode()
    assert "/admissions/settings/" in login(erp.admin).get("/settings/").content.decode()


def test_switching_the_module_off_hides_it_again(erp, admissions):
    erp.school.admissions_enabled = False
    erp.school.save()
    assert login(erp.staff).get("/admissions/").status_code == 404
    assert 'href="/admissions/"' not in login(erp.staff).get("/").content.decode()
