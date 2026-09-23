from datetime import date

from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_to_database,
)


def parse_date_argument(value, name):
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CommandError(
            f"Invalid --{name} value. Use YYYY-MM-DD."
        ) from error


class Command(BaseCommand):
    help = (
        "Collect GA4 website analytics and save "
        "them directly to the Django database. "
        "Safe to re-run: existing ranges are replaced, "
        "never duplicated."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--start",
            help=(
                "Collect one exact inclusive range "
                "starting on this date (YYYY-MM-DD). "
                "Requires --end."
            ),
        )

        parser.add_argument(
            "--end",
            help="Inclusive end date (YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        start_date = None
        end_date = None

        if options["start"] or options["end"]:
            if not (options["start"] and options["end"]):
                raise CommandError(
                    "--start and --end must be used together."
                )

            start_date = parse_date_argument(options["start"], "start")
            end_date = parse_date_argument(options["end"], "end")

            if start_date > end_date:
                raise CommandError(
                    "--start must be on or before --end."
                )

        self.stdout.write(
            "Starting GA4 database collection..."
        )

        try:
            result = collect_ga4_to_database(
                start_date=start_date,
                end_date=end_date,
            )

        except Exception as error:
            raise CommandError(
                f"GA4 collection failed: {error}"
            ) from error

        ranges = "\n".join(
            f"  {range_start} to {range_end} [{period_key}]"
            for range_start, range_end, period_key in result["ranges"]
        )

        self.stdout.write(
            self.style.SUCCESS(
                "\nGA4 collection completed.\n"
                f"Property: {result['property']} "
                f"({result['property_timezone']})\n"
                f"Daily metrics: "
                f"{result['start_date']} to "
                f"{result['end_date']}\n"
                f"Range reports:\n{ranges}\n"
                f"DB rows written: {result['processed']}"
            )
        )
