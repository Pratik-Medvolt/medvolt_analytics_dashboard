import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import (
    BaseCommand,
    CommandError,
)


class Command(BaseCommand):
    help = (
        "Write a consistent snapshot of the SQLite database to a backup "
        "file and prune snapshots older than the retention window."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=None,
            help=(
                "Directory to write the snapshot into. Defaults to a "
                "'backups' directory next to the live database file."
            ),
        )

        parser.add_argument(
            "--retain-days",
            type=int,
            default=14,
            help="Delete snapshots older than this many days (0 disables pruning).",
        )

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]

        if "sqlite3" not in database["ENGINE"]:
            raise CommandError(
                "backup_db only supports the SQLite backend; this project "
                f"is configured with {database['ENGINE']}."
            )

        source_path = Path(database["NAME"])

        if not source_path.exists():
            raise CommandError(
                f"Database file not found at {source_path}. Has migrate run?"
            )

        output_dir = (
            Path(options["output_dir"])
            if options["output_dir"]
            else source_path.parent / "backups"
        )

        output_dir.mkdir(parents=True, exist_ok=True)

        target_path = (
            output_dir
            / f"db-{datetime.now().strftime('%Y%m%dT%H%M%S')}.sqlite3"
        )

        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)

        try:
            destination = sqlite3.connect(str(target_path))

            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

        size_mb = target_path.stat().st_size / (1024 * 1024)

        self.stdout.write(
            self.style.SUCCESS(
                f"Backup written: {target_path} ({size_mb:.2f} MB)"
            )
        )

        pruned = self.prune(output_dir, options["retain_days"])

        if pruned:
            self.stdout.write(
                f"Pruned {pruned} snapshot(s) older than "
                f"{options['retain_days']} day(s)."
            )

    def prune(self, output_dir, retain_days):
        if retain_days <= 0:
            return 0

        cutoff = date.today() - timedelta(days=retain_days)
        pruned = 0

        for candidate in output_dir.glob("db-*.sqlite3"):
            modified = date.fromtimestamp(candidate.stat().st_mtime)

            if modified < cutoff:
                candidate.unlink()
                pruned += 1

        return pruned
