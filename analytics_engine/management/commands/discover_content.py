from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.content_discovery_db import (
    discover_content_to_database,
)


class Command(BaseCommand):
    help = (
        "Discover website pages and MailerLite "
        "campaigns and save them to the Content table."
    )

    def handle(self, *args, **options):
        self.stdout.write(
            "Starting content discovery..."
        )

        try:
            result = (
                discover_content_to_database()
            )

        except Exception as error:
            raise CommandError(
                f"Content discovery failed: {error}"
            ) from error

        website = result["website"]
        mailerlite = result["mailerlite"]

        self.stdout.write(
            "\nWebsite discovery"
        )

        self.stdout.write(
            f"Found: {website['found']}"
        )

        self.stdout.write(
            f"Created: {website['created']}"
        )

        self.stdout.write(
            f"Updated: {website['updated']}"
        )

        self.stdout.write(
            f"Failed: {website['failed']}"
        )

        self.stdout.write(
            "\nMailerLite discovery"
        )

        self.stdout.write(
            f"Found: {mailerlite['found']}"
        )

        self.stdout.write(
            f"Created: {mailerlite['created']}"
        )

        self.stdout.write(
            f"Updated: {mailerlite['updated']}"
        )

        self.stdout.write(
            f"Failed: {mailerlite['failed']}"
        )

        self.stdout.write(
            self.style.SUCCESS(
                "\nContent discovery completed."
            )
        )