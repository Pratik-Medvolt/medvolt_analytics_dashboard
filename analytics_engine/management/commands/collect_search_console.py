from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.search_console_db_collector import (
    collect_search_console_to_database,
)


class Command(BaseCommand):
    help = (
        "Collect Google Search Console analytics "
        "and save them to the Django database."
    )

    def handle(self, *args, **options):
        self.stdout.write(
            "Starting Search Console "
            "database collection..."
        )

        try:
            result = (
                collect_search_console_to_database()
            )

        except Exception as error:
            raise CommandError(
                "Search Console collection "
                f"failed: {error}"
            ) from error

        self.stdout.write(
            self.style.SUCCESS(
                "\nSearch Console collection "
                "completed.\n"
                f"Property: "
                f"{result['property']}\n"
                f"Period: "
                f"{result['start_date']} to "
                f"{result['end_date']}\n"
                f"Processed: "
                f"{result['processed']}\n"
                f"Created: "
                f"{result['created']}\n"
                f"Updated: "
                f"{result['updated']}\n"
                f"Failed: "
                f"{result['failed']}"
            )
        )