"""
SQLite set up for a web server: WAL journalling, write transactions that queue, a longer wait
when busy, and the database file where SQLITE_PATH says.

Checked in a separate process against a real file, because the test database lives in memory.
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

from django.db import connection

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import django, os, sys
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()
from django.conf import settings
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
print(settings.DATABASES["default"]["NAME"], mode, connection.transaction_mode)
"""


def run_probe(tmp_path):
    env = {**os.environ, "SQLITE_PATH": str(tmp_path / "school.sqlite3"), "DJANGO_DEBUG": "1"}
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().split()


def test_the_database_file_goes_where_sqlite_path_says_in_wal_mode(tmp_path):
    name, mode, transaction_mode = run_probe(tmp_path)
    assert Path(name) == tmp_path / "school.sqlite3"
    assert mode == "wal"
    assert transaction_mode == "IMMEDIATE"
    assert (tmp_path / "school.sqlite3").exists()


def test_the_test_database_uses_the_same_transaction_rules():
    options = connection.settings_dict["OPTIONS"]
    assert options["transaction_mode"] == "IMMEDIATE"
    assert options["timeout"] >= 20


def test_two_writers_queue_instead_of_failing(tmp_path):
    """Two processes' worth of writes at once on a file database: both land, neither errors."""
    import sqlite3

    path = tmp_path / "busy.sqlite3"
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("create table receipts (n integer)")
    setup.commit()
    setup.close()
    errors = []

    def writer():
        # The same settings the app uses: a 20-second wait and BEGIN IMMEDIATE.
        con = sqlite3.connect(path, timeout=20, isolation_level=None)
        try:
            for _ in range(50):
                con.execute("BEGIN IMMEDIATE")
                (last,) = con.execute("select coalesce(max(n), 0) from receipts").fetchone()
                con.execute("insert into receipts values (?)", (last + 1,))
                con.execute("COMMIT")
        except sqlite3.OperationalError as exc:  # "database is locked"
            errors.append(str(exc))
        finally:
            con.close()

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = sqlite3.connect(path)
    numbers = [n for (n,) in check.execute("select n from receipts order by n")]
    check.close()
    assert not errors
    # Read-then-write under the write lock: 200 receipts numbered 1..200, no duplicates.
    assert numbers == list(range(1, 201))
