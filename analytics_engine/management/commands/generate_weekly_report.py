from datetime import datetime

from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.services.weekly_report_service import (
    generate_weekly_report,
    get_previous_completed_week,
)


class Command(BaseCommand):
    help = (
        "Generate or update a weekly analytics "
        "report."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--week-start",
            type=str,
            default=None,
            help=(
                "Optional Monday date in YYYY-MM-DD "
                "format. When omitted, the previous "
                "completed week is generated."
            ),
        )

    def handle(self, *args, **options):
        week_start_value = options.get(
            "week_start"
        )

        if week_start_value:
            try:
                week_start = (
                    datetime.strptime(
                        week_start_value,
                        "%Y-%m-%d",
                    )
                    .date()
                )

            except ValueError as error:
                raise CommandError(
                    "Invalid --week-start value. "
                    "Use YYYY-MM-DD."
                ) from error

            if week_start.weekday() != 0:
                raise CommandError(
                    "--week-start must be a Monday."
                )
        else:
            week_start, _ = (
                get_previous_completed_week()
            )

        self.stdout.write(
            "\nGenerating weekly analytics report..."
        )

        self.stdout.write(
            f"Week starting: {week_start}"
        )

        try:
            result = generate_weekly_report(
                week_start=week_start
            )

        except Exception as error:
            raise CommandError(
                "Weekly report generation failed: "
                f"{error}"
            ) from error

        action = (
            "Created"
            if result["created"]
            else "Updated"
        )

        self.stdout.write(
            self.style.SUCCESS(
                "\nWeekly report completed.\n"
                f"Action: {action}\n"
                f"Period: "
                f"{result['week_start']} to "
                f"{result['week_end']}\n"
                f"Website views: "
                f"{result['website_views']}\n"
                f"Website sessions: "
                f"{result['website_sessions']}\n"
                f"Search clicks: "
                f"{result['search_clicks']}\n"
                f"Search impressions: "
                f"{result['search_impressions']}\n"
                f"Email campaigns: "
                f"{result['email_campaigns']}\n"
                f"Email opens: "
                f"{result['email_opens']}\n"
                f"Email clicks: "
                f"{result['email_clicks']}\n"
                f"Average open rate: "
                f"{result['email_open_rate']:.2f}%\n"
                f"Average click rate: "
                f"{result['email_click_rate']:.2f}%"
            )
        )