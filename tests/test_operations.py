"""Health check, backups, the generated role matrix and the deployment checklist."""

import json
import tarfile
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client


def test_healthz_reports_the_database_and_cache(erp):
    response = Client().get("/healthz/")
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["status"] == "ok"
    assert payload["checks"]["database"] == "ok"
    assert payload["checks"]["cache"] == "ok"


def test_healthz_needs_no_sign_in(erp):
    """A probe that requires a session cannot tell a load balancer anything useful."""
    assert Client().get("/healthz/").status_code == 200


def test_backup_writes_the_database_media_and_restore_notes(erp, settings, tmp_path):
    media = tmp_path / "media"
    (media / "students").mkdir(parents=True)
    (media / "students" / "photo.txt").write_text("a photo", encoding="utf-8")
    settings.MEDIA_ROOT = media

    output = tmp_path / "backups"
    call_command("backup", output=str(output), verbosity=0)

    folders = list(output.iterdir())
    assert len(folders) == 1
    folder = folders[0]
    dump = folder / "database.sql"
    assert dump.exists()
    assert "CREATE TABLE" in dump.read_text(encoding="utf-8")
    assert (folder / "media.tar.gz").exists()

    notes = (folder / "RESTORE.txt").read_text(encoding="utf-8")
    assert "To restore" in notes
    assert "manage.py migrate" in notes
    assert "Rehearse this" in notes

    with tarfile.open(folder / "media.tar.gz") as archive:
        assert any(name.endswith("photo.txt") for name in archive.getnames())


def test_backup_prunes_older_runs(erp, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    output = tmp_path / "backups"
    output.mkdir()
    for stamp in ("20260101-010101", "20260102-010101", "20260103-010101"):
        (output / stamp).mkdir()

    call_command("backup", output=str(output), keep=2, skip_media=True, verbosity=0)
    remaining = sorted(path.name for path in output.iterdir())
    assert len(remaining) == 2
    assert "20260101-010101" not in remaining


def test_backup_can_skip_media(erp, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    output = tmp_path / "backups"
    call_command("backup", output=str(output), skip_media=True, verbosity=0)
    folder = next(iter(output.iterdir()))
    assert not (folder / "media.tar.gz").exists()


def test_role_matrix_lists_every_role_and_marks_access(erp, capsys):
    call_command("role_matrix")
    printed = capsys.readouterr().out
    for role in ("Administrator", "Principal", "Accountant", "Teacher", "Staff", "Student", "Guardian"):
        assert role in printed
    assert "/students/" in printed
    assert "students.view_student" in printed
    assert "Y = allowed" in printed


def test_role_matrix_has_a_plain_format(erp, capsys):
    call_command("role_matrix", format="plain")
    printed = capsys.readouterr().out
    assert "|" not in printed.splitlines()[0]
    assert "/fees/" in printed


def test_deploy_checklist_is_clean(settings):
    """`manage.py check --deploy` must pass before a release, so it is a test."""
    from io import StringIO

    settings.DEBUG = False
    settings.SECRET_KEY = "a-long-enough-random-value-for-this-check-0123456789abcdef"
    settings.ALLOWED_HOSTS = ["school.example"]
    settings.SESSION_COOKIE_SECURE = True
    settings.CSRF_COOKIE_SECURE = True
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_HSTS_SECONDS = 31536000
    settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    settings.SECURE_HSTS_PRELOAD = True
    settings.SECURE_CONTENT_TYPE_NOSNIFF = True
    settings.X_FRAME_OPTIONS = "DENY"

    output = StringIO()
    call_command("check", deploy=True, fail_level="WARNING", stdout=output, stderr=output)
    assert "issues" not in output.getvalue() or "no issues" in output.getvalue()


def test_form_field_limit_allows_a_whole_class_grid(settings):
    """A mark grid or a timetable week posts one field per cell."""
    assert settings.DATA_UPLOAD_MAX_NUMBER_FIELDS >= 5000


def test_whitenoise_serves_static_files_in_production_only(monkeypatch):
    """In development Django's own staticfiles app does it, so the extra layer is noise."""
    import runpy

    from django.conf import settings as live

    assert "whitenoise.middleware.WhiteNoiseMiddleware" not in live.MIDDLEWARE

    monkeypatch.setenv("DJANGO_DEBUG", "0")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "a-long-enough-value-for-this-check-0123456789abcdef")
    production = runpy.run_module("config.settings")
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in production["MIDDLEWARE"]
    assert "whitenoise" in production["STORAGES"]["staticfiles"]["BACKEND"]


@pytest.mark.parametrize("name", ["Dockerfile", "docker-compose.yml", ".github/workflows/ci.yml"])
def test_deployment_files_are_present(name):
    assert (Path(__file__).resolve().parents[1] / name).exists()


def test_setup_school_command_runs_for_a_single_school(erp, capsys):
    from fees.models import FeeCategory

    erp.other.delete()
    call_command("setup_school")
    printed = capsys.readouterr().out
    assert "ready to use" in printed
    assert FeeCategory.objects.filter(school=erp.school, name="Tuition Fee").exists()


def test_setup_school_refuses_to_guess_between_schools(erp):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="pass --slug"):
        call_command("setup_school", verbosity=0)
