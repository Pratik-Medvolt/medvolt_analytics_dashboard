from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.mailerlite_db_collector import (
    collect_mailerlite_to_database,
)


class Command(BaseCommand):
    help = (
        "Collect MailerLite campaign analytics "
        "and save them directly to the Django database."
    )

    def handle(self, *args, **options):

        self.stdout.write(
            "Starting MailerLite database collection..."
        )

        try:
            result = collect_mailerlite_to_database()

        except Exception as error:
            raise CommandError(
                f"MailerLite collection failed: {error}"
            ) from error

        self.stdout.write(
            self.style.SUCCESS(
                "\nMailerLite collection completed.\n"
                f"Processed: {result['processed']}\n"
                f"Created: {result['created']}\n"
                f"Updated: {result['updated']}\n"
                f"Failed: {result['failed']}"
            )
        )