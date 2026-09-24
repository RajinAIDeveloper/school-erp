"""
Phase 5: homework is a module the platform administrator gives a school.

A school without it never meets it: every homework address answers "not found" for every
role, administrators included, and no page mentions homework. With it, only the people the
work concerns get in.
"""

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import URLPattern, reverse

from homework import urls as homework_urls
from users.models import User


def login(user):
    client = Client()
    client.force_login(user)
    return client


def everyone(erp):
    def add(username, role):
        user = User.objects.create_user(username=username, school=erp.school, password="Test-pass-9842")
        user.groups.add(Group.objects.get(name=role))
        return user

    student_user = add("pupil", "Student")
    erp.student.user = student_user
    erp.student.save()
    return {
        "Administrator": erp.admin,
        "Principal": add("principal", "Principal"),
        "Vice Principal": add("vice", "Vice Principal"),
        "Accountant": erp.accountant,
        "Teacher": erp.teacher,
        "Staff": erp.staff,
        "Student": student_user,
        "Guardian": erp.parent,
    }


def homework_addresses():
    for pattern in homework_urls.urlpatterns:
        assert isinstance(pattern, URLPattern)
        kwargs = {name: 1 for name in pattern.pattern.converters}
        yield reverse(f"homework:{pattern.name}", kwargs=kwargs)


def test_every_homework_address_is_not_found_for_everyone_without_the_module(erp):
    assert not erp.school.homework_enabled
    for role, user in everyone(erp).items():
        client = login(user)
        for url in homework_addresses():
            assert client.get(url).status_code == 404, (role, url)
            assert client.post(url).status_code in (404, 405), (role, url)


def test_signed_out_visitors_are_sent_to_sign_in(erp):
    for url in homework_addresses():
        response = Client().get(url)
        assert response.status_code == 302 and "/login/" in response["Location"], url


@pytest.mark.parametrize("page", ["/", "/portal/", "/settings/", "/settings/notifications/"])
def test_no_page_mentions_homework_without_the_module(erp, page):
    for user in everyone(erp).values():
        response = login(user).get(page, follow=True)
        body = response.content.decode()
        assert "Homework" not in body and "বাড়ির কাজ" not in body and "/homework/" not in body, (user, page)


def test_with_the_module_each_role_reaches_only_its_own_part(erp, homework):
    people = everyone(erp)
    for role in ("Administrator", "Principal", "Vice Principal", "Teacher"):
        assert login(people[role]).get("/homework/").status_code == 200, role
    for role in ("Accountant", "Staff", "Student", "Guardian"):
        assert login(people[role]).get("/homework/").status_code == 403, role
    for role in ("Student", "Guardian"):
        assert login(people[role]).get("/homework/todo/").status_code == 200, role
    for role in ("Accountant", "Staff", "Teacher"):
        assert login(people[role]).get("/homework/todo/").status_code == 403, role
    # The daily limits are the managers' to set.
    assert login(people["Teacher"]).get("/homework/limits/").status_code == 403
    assert login(people["Vice Principal"]).get("/homework/limits/").status_code == 200


def test_with_the_module_it_appears_where_it_belongs(erp, homework):
    staff_nav = login(erp.teacher).get("/").content.decode()
    assert 'href="/homework/"' in staff_nav
    family = login(erp.parent).get("/portal/").content.decode()
    assert 'href="/homework/todo/"' in family and "due this week" in family
    hub = login(erp.admin).get("/settings/").content.decode()
    assert "/homework/limits/" in hub


def test_switching_the_module_off_hides_it_again(erp, homework):
    erp.school.homework_enabled = False
    erp.school.save()
    assert login(erp.teacher).get("/homework/").status_code == 404
    assert "/homework/" not in login(erp.parent).get("/portal/").content.decode()
