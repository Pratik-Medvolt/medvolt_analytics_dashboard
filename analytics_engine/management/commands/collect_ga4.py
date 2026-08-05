from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_to_database,
)


class Command(BaseCommand):
    help = (
        "Collect GA4 website analytics and save "
        "them directly to the Django database."
    )

    def handle(self, *args, **options):
        self.stdout.write(
            "Starting GA4 database collection..."
        )

        try:
            result = collect_ga4_to_database()

        except Exception as error:
            raise CommandError(
                f"GA4 collection failed: {error}"
            ) from error

        self.stdout.write(
            self.style.SUCCESS(
                "\nGA4 collection completed.\n"
                f"Period: "
                f"{result['start_date']} to "
                f"{result['end_date']}\n"
                f"Processed: {result['processed']}\n"
                f"Created: {result['created']}\n"
                f"Updated: {result['updated']}\n"
                f"Failed: {result['failed']}"
            )
        )