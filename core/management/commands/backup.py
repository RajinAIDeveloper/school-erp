"""
Take a backup a school could actually restore from.

A school ERP holds the only copy of a child's results and a family's payment history.
This writes the database and the uploaded files into one timestamped folder, prunes old
ones, and says exactly how to put them back — a backup nobody knows how to restore is
not a backup.
"""

import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Back up the database and the media folder into a timestamped archive."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", default=str(Path(settings.BASE_DIR) / "backups"), help="Where to write the archive."
        )
        parser.add_argument("--keep", type=int, default=14, help="How many previous backups to keep.")
        parser.add_argument("--skip-media", action="store_true", help="Database only.")

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
        self.stdout.write("Restore instructions are in that folder's RESTORE.txt.")

    def dump_database(self, folder):
        config = settings.DATABASES["default"]
        engine = config["ENGINE"]
        if "sqlite" in engine:
            # A dump through the live connection rather than a file copy: copying a
            # database that is being written to can capture a torn state that will not
            # open again, and a dump restores the same way a PostgreSQL one does.
            from django.db import connection

            target = folder / "database.sql"
            connection.ensure_connection()
            with target.open("w", encoding="utf-8") as handle:
                for statement in connection.connection.iterdump():
                    handle.write(statement)
                    handle.write("\n")
            return target
        if "postgresql" in engine:
            target = folder / "database.sql"
            url = (
                f"postgresql://{config['USER']}:{config['PASSWORD']}@{config['HOST']}:{config['PORT']}/{config['NAME']}"
            )
            try:
                with target.open("wb") as handle:
                    subprocess.run(["pg_dump", url], stdout=handle, check=True)
            except FileNotFoundError as exc:
                raise CommandError("pg_dump is not on PATH; install the PostgreSQL client tools.") from exc
            except subprocess.CalledProcessError as exc:
                raise CommandError(f"pg_dump failed: {exc}") from exc
            # Never leave credentials in a file the backup folder might be shared from.
            _ = urlparse(url)
            return target
        raise CommandError(f"Backing up {engine} is not supported here.")

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
        if media:
            lines.append(f"  {media.name} - uploaded photos, documents and files")
        lines += [
            "",
            "To restore:",
            "  1. Stop the application.",
            "  2. Database:",
            "       SQLite     - sqlite3 db.sqlite3 < database.sql  (against an empty file)",
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
