from pathlib import Path

import pytest


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_desktop_mobile_navigation_and_real_forms(live_server, erp, settings):
    playwright = pytest.importorskip("playwright.sync_api")
    settings.ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - the browser binary is optional for this suite
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(live_server.url + "/login/")
        page.locator('input[name="username"]').fill("admin")
        page.locator('input[name="password"]').fill("Test-pass-9842")
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(live_server.url + "/")
        # LiveServer does not serve static files; route assets from the workspace.
        # Screenshots exercise the actual generated CSS and client JS.
        root = Path(__file__).resolve().parents[1]
        page.route(
            "**/static/**",
            lambda route: route.fulfill(path=str(root / "static" / route.request.url.split("/static/", 1)[1])),
        )
        page.reload()
        heading = page.locator("h1").inner_text()
        assert heading.startswith("Dashboard")
        # The header carries the date and session; the separator must not be mojibake.
        assert "·" in heading and "�" not in heading
        page.goto(live_server.url + "/students/")
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("Ayesha", exact=True).count() == 1
        assert (
            page.locator("aside").evaluate("(e)=>getComputedStyle(e).backgroundColor") == "rgb(15, 23, 43)"
            or page.locator("aside").evaluate("(e)=>getComputedStyle(e).backgroundColor") != "rgba(0, 0, 0, 0)"
        )
        output = root / "docs" / "screenshots"
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "students-desktop.png"), full_page=True)
        page.get_by_role("link", name="+ New", exact=True).click()
        page.locator('input[name="student_id"]').fill("BROWSER-1")
        page.locator('input[name="first_name"]').fill("Browser Student")
        page.locator('select[name="gender"]').select_option("F")
        page.locator('input[name="date_of_birth"]').fill("2016-01-01")
        page.locator('input[name="admission_date"]').fill("2026-01-01")
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_url(live_server.url + "/students/")
        assert page.get_by_text("Browser Student", exact=True).count() == 1
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        page.get_by_role("button", name="Menu", exact=True).click()
        page.wait_for_timeout(250)
        assert page.locator("#sidebar").bounding_box()["x"] >= -1
        page.screenshot(path=str(output / "navigation-mobile.png"), full_page=True)
        page.locator("#sidebar-backdrop").click(position={"x": 380, "y": 700})
        page.goto(live_server.url + f"/exams/results/?exam={erp.exam.pk}&class_level={erp.level.pk}")
        page.screenshot(path=str(output / "results-mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
