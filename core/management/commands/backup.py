"""
Take a backup a school could actually restore from.

A school ERP holds the only copy of a child's results and a family's payment history.
This writes the database and the uploaded files into one timestamped folder, prunes old
ones, and says exactly how to put them back — a backup nobody knows how to restore is
not a backup.
"""

import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

# SQLite's backup waits a quarter of a second each time the database is locked: about a minute.
BUSY_WAITS = 240


class Command(BaseCommand):
    help = "Back up the database and the media folder into a timestamped archive."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", default=str(Path(settings.BASE_DIR) / "backups"), help="Where to write the archive."
        )
        parser.add_argument("--keep", type=int, default=14, help="How many previous backups to keep.")
        parser.add_argument("--skip-media", action="store_true", help="Database only.")
        parser.add_argument(
            "--copy-to",
            help=(
                "A second place to keep each backup: a USB drive, a network share or a folder a cloud "
                "service keeps in sync. A backup on the same disk as the database is lost with the disk."
            ),
        )

    def handle(self, *args, **options):
        destination = Path(options["output"])
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = destination / stamp
        folder.mkdir()

        database = self.dump_database(folder)
        media = None if options["skip_media"] else self.archive_media(folder)
        self.write_readme(folder, database, media)
        self.prune(destination, options["keep"])
        self.stdout.write(self.style.SUCCESS(f"Backup written to {folder}"))

        if options["copy_to"]:
            elsewhere = Path(options["copy_to"])
            try:
                elsewhere.mkdir(parents=True, exist_ok=True)
                shutil.copytree(folder, elsewhere / folder.name)
            except OSError as exc:
                # The local backup is good; say plainly that the second copy is missing.
                raise CommandError(f"The backup is in {folder}, but copying it to {elsewhere} failed: {exc}") from exc
            self.prune(elsewhere, options["keep"])
            self.stdout.write(self.style.SUCCESS(f"Copied to {elsewhere / folder.name}"))
        self.stdout.write("Restore instructions are in that folder's RESTORE.txt.")

    def dump_database(self, folder):
        config = settings.DATABASES["default"]
        engine = config["ENGINE"]
        if "sqlite" in engine:
            return self.dump_sqlite(folder)
        if "postgresql" in engine:
            target = folder / "database.sql"
            # Arguments, not a URI. A connection URI puts the password in the process
            # list, where every other user on the machine can read it, and it also has to
            # be percent-encoded: a password containing @ or / silently builds a URI that
            # points somewhere else. PGPASSWORD is read by libpq and never appears in ps.
            command = [
                "pg_dump",
                "--no-password",
                "--format=plain",
                f"--host={config['HOST'] or 'localhost'}",
                f"--port={config['PORT'] or 5432}",
                f"--username={config['USER']}",
                f"--dbname={config['NAME']}",
            ]
            environment = {**os.environ}
            if config.get("PASSWORD"):
                environment["PGPASSWORD"] = config["PASSWORD"]
            try:
                with target.open("wb") as handle:
                    subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, env=environment, check=True)
            except FileNotFoundError as exc:
                raise CommandError("pg_dump is not on PATH; install the PostgreSQL client tools.") from exc
            except subprocess.CalledProcessError as exc:
                # The message may quote the connection; the password is not in it, but
                # keep the report to what pg_dump itself said.
                detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
                raise CommandError(f"pg_dump failed: {detail or exc}") from exc
            return target
        raise CommandError(f"Backing up {engine} is not supported here.")

    def dump_sqlite(self, folder):
        """
        One consistent snapshot, taken with SQLite's own backup API.

        Copying the file while the school is using it can capture a half-written state, and
        dumping table by table can pair a receipt from before a payment with an invoice from
        after it. The backup API copies every page as of one moment, even in WAL mode with
        people still working. The copy is checked before it counts, and a plain SQL dump is
        written from the copy for anyone who needs to read or move the data.

        It reads through a connection of its own, so it takes what has been committed and
        never waits on a transaction of this process. If the database stays locked for a
        minute it gives up and says so, rather than hanging a scheduled job.
        """
        import sqlite3

        from django.db import connection

        params = connection.get_connection_params()
        snapshot = folder / "database.sqlite3"
        source = sqlite3.connect(params["database"], uri=params.get("uri", False), timeout=20)
        copy = sqlite3.connect(snapshot)
        waits = []

        def progress(status, remaining, total):
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                waits.append(status)
                if len(waits) > BUSY_WAITS:
                    raise CommandError("The database stayed locked, so no backup was taken. Try again shortly.")

        try:
            source.backup(copy, progress=progress)
            (verdict,) = copy.execute("PRAGMA integrity_check").fetchone()
            if verdict != "ok":
                raise CommandError(f"The backup copy failed its integrity check: {verdict}")
            with (folder / "database.sql").open("w", encoding="utf-8") as handle:
                for statement in copy.iterdump():
                    handle.write(statement)
                    handle.write("\n")
        finally:
            copy.close()
            source.close()
        return snapshot

    def archive_media(self, folder):
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.exists():
            return None
        target = folder / "media.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            archive.add(media_root, arcname="media")
        return target

    def write_readme(self, folder, database, media):
        lines = [
            "School ERP backup",
            f"Taken: {datetime.now():%d %B %Y at %H:%M}",
            "",
            "Contents:",
            f"  {database.name} - the database",
        ]
        if database.suffix == ".sqlite3":
            lines.append("  database.sql - the same snapshot as SQL statements")
        if media:
            lines.append(f"  {media.name} - uploaded photos, documents and files")
        lines += [
            "",
            "To restore:",
            "  1. Stop the application.",
            "  2. Database:",
            "       SQLite     - copy database.sqlite3 to the database's path (SQLITE_PATH) and delete",
            "                    any -wal and -shm files beside it; or: sqlite3 new.sqlite3 < database.sql",
            "       PostgreSQL - createdb, then: psql <database> < database.sql",
            "  3. Media: extract media.tar.gz so that its 'media' folder replaces MEDIA_ROOT.",
            "  4. Run: python manage.py migrate",
            "  5. Start the application and sign in to confirm the data is there.",
            "",
            "Rehearse this on a spare machine before you need it.",
        ]
        (folder / "RESTORE.txt").write_text("\n".join(lines), encoding="utf-8")

    def prune(self, destination, keep):
        if keep <= 0:
            return
        folders = sorted(
            (path for path in destination.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        for stale in folders[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
            self.stdout.write(f"  removed old backup {stale.name}")
