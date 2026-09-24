"""
The public result lookup: off unless the school allows it, opened by the student ID and the
random result code on the admit card (never the date of birth), published results only, and
turned away after too many wrong attempts.
"""

from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import Client

from examinations.public import ATTEMPTS, ensure_codes, reissue_code
from examinations.services import publish_exam, save_mark


@pytest.fixture(autouse=True)
def fresh_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def lookup_on(erp):
    erp.school.public_results_enabled = True
    erp.school.save()
    ensure_codes([erp.student])
    erp.student.refresh_from_db()
    return erp


def url(erp):
    return f"/results/{erp.school.slug}/"


def ask(erp, student_id=None, code=None, client=None):
    return (client or Client()).post(
        url(erp), {"student_id": student_id or erp.student.student_id, "code": code or erp.student.result_code}
    )


def test_the_lookup_is_off_until_the_school_switches_it_on(erp):
    assert Client().get(url(erp)).status_code == 404
    erp.school.public_results_enabled = True
    erp.school.save()
    page = Client().get(url(erp))
    assert page.status_code == 200 and page["Cache-Control"] == "no-store"
    assert b"Result code" in page.content


def test_codes_are_random_readable_and_unique_in_the_school(erp, lookup_on):
    code = erp.student.result_code
    assert len(code) == 9 and code[4] == "-"
    assert not set(code.replace("-", "")) & set("01OI")
    ensure_codes([erp.student])
    erp.student.refresh_from_db()
    assert erp.student.result_code == code  # never changed by printing again


def test_the_right_pair_shows_only_published_results(erp, lookup_on):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    body = ask(erp).content.decode()
    assert "No results have been published" in body  # the exam is still a draft
    publish_exam(erp.exam, erp.admin)
    body = ask(erp, code=erp.student.result_code.lower().replace("-", " ")).content.decode()
    assert "Term 1" in body and "Math" in body and "Ayesha" in body


def test_a_wrong_pair_says_only_that_nothing_matched(erp, lookup_on):
    body = ask(erp, code="ABCD-EFGH").content.decode()
    assert "No result matches" in body and "Ayesha" not in body
    body = ask(erp, student_id="nobody").content.decode()
    assert "No result matches" in body


def test_the_date_of_birth_does_not_open_it(erp, lookup_on):
    body = ask(erp, code=erp.student.date_of_birth.strftime("%d%m%Y")).content.decode()
    assert "No result matches" in body


def test_too_many_wrong_attempts_are_turned_away_even_with_the_right_code(erp, lookup_on):
    client = Client()
    for _ in range(ATTEMPTS):
        ask(erp, code="WRNG-CODE", client=client)
    body = ask(erp, client=client).content.decode()
    assert "Too many attempts" in body and "Ayesha" not in body


def test_another_schools_page_does_not_find_this_student(erp, lookup_on):
    erp.other.public_results_enabled = True
    erp.other.save()
    response = Client().post(
        f"/results/{erp.other.slug}/", {"student_id": erp.student.student_id, "code": erp.student.result_code}
    )
    assert "No result matches" in response.content.decode()


def test_reissuing_a_code_stops_the_old_one(erp, lookup_on):
    old = erp.student.result_code
    new = reissue_code(user=erp.admin, student=erp.student)
    assert new != old
    assert "No result matches" in ask(erp, code=old).content.decode()
    teacher = Client()
    teacher.force_login(erp.teacher)
    assert teacher.post(f"/students/{erp.student.pk}/result-code/").status_code == 403


def test_printing_admit_cards_issues_codes_when_the_lookup_is_on(erp):
    erp.school.public_results_enabled = True
    erp.school.save()
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/exams/admit-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}")
    assert response.status_code == 200
    erp.student.refresh_from_db()
    assert erp.student.result_code
    page = client.get(f"/students/{erp.student.pk}/").content.decode()
    assert erp.student.result_code in page and "Issue a new code" in page
