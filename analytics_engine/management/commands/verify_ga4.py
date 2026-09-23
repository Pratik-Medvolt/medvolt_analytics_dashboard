from datetime import date

from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_range,
)
from analytics_engine.collectors.ga4_reports import (
    GA4_PROPERTY_ID,
    create_ga4_client,
    fetch_ga4_channel_groups,
    fetch_ga4_daily_metrics,
    fetch_ga4_page_performance,
    fetch_ga4_summary,
    fetch_ga4_traffic_sources,
)
from analytics_engine.models import (
    GA4DailyMetric,
    GA4Report,
)
from analytics_engine.services.website_dashboard import (
    build_website_dashboard_payload,
    get_range_reports,
)


SUMMARY_METRICS = [
    ("Views", "views"),
    ("Sessions", "sessions"),
    ("Total Users", "total_users"),
    ("Active Users", "active_users"),
    ("New Users", "new_users"),
    ("Engaged Sessions", "engaged_sessions"),
    ("Event Count", "event_count"),
    ("Engagement Duration (s)", "engagement_duration_seconds"),
]


def ratio(numerator, denominator):
    if not denominator or numerator is None:
        return None
    return numerator / denominator


def format_value(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def values_match(*values):
    rounded = {
        round(value, 2) if isinstance(value, float) else value
        for value in values
    }
    return len(rounded) == 1


class Command(BaseCommand):
    help = (
        "Compare raw GA4 Data API values with the database and "
        "the Website Analytics dashboard data for one date range."
    )

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="YYYY-MM-DD")
        parser.add_argument("--end", required=True, help="YYYY-MM-DD")

        parser.add_argument(
            "--collect",
            action="store_true",
            help="Collect this range from GA4 into the database first.",
        )

        parser.add_argument(
            "--top",
            type=int,
            default=10,
            help="Number of pages to compare (default 10).",
        )

    def handle(self, *args, **options):
        try:
            start_date = date.fromisoformat(options["start"])
            end_date = date.fromisoformat(options["end"])
        except ValueError as error:
            raise CommandError("Dates must be YYYY-MM-DD.") from error

        if start_date > end_date:
            raise CommandError("--start must be on or before --end.")

        self.mismatches = 0

        client = create_ga4_client()

        if options["collect"]:
            result = collect_ga4_range(start_date, end_date, client=client)
            self.stdout.write(
                f"Collected range: {result['rows_written']} DB rows written\n"
            )

        ga4_summary = fetch_ga4_summary(client, start_date, end_date)
        ga4_sources = fetch_ga4_traffic_sources(client, start_date, end_date)
        ga4_channels = fetch_ga4_channel_groups(client, start_date, end_date)
        ga4_pages = fetch_ga4_page_performance(client, start_date, end_date)
        ga4_daily = fetch_ga4_daily_metrics(client, start_date, end_date)

        reports = get_range_reports(start_date, end_date)
        dashboard = build_website_dashboard_payload(start_date, end_date)

        self.stdout.write(f"GA4 PROPERTY:  {GA4_PROPERTY_ID}")
        self.stdout.write(
            f"TIMEZONE:      {ga4_summary['property_timezone']}"
        )
        self.stdout.write(
            f"DATE RANGE:    {start_date} -> {end_date} "
            f"({(end_date - start_date).days + 1} days, inclusive)"
        )

        if not reports:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis range is not in the database. "
                    "Re-run with --collect to store it."
                )
            )

        self.compare_summary(ga4_summary, reports, dashboard)
        self.compare_dimension(
            "TRAFFIC ACQUISITION (sessions by source / medium)",
            ga4_sources["rows"],
            reports.get(GA4Report.REPORT_SOURCE_MEDIUM),
            {
                row["source_medium"]: row
                for row in dashboard["traffic_sources"]
            },
            ["sessions", "engaged_sessions"],
        )
        self.compare_dimension(
            "DEFAULT CHANNEL GROUP (sessions)",
            ga4_channels["rows"],
            reports.get(GA4Report.REPORT_CHANNEL_GROUP),
            {
                row["channel_group"]: row
                for row in dashboard["channel_groups"]
            },
            ["sessions"],
        )
        self.compare_dimension(
            f"TOP {options['top']} PAGES (by views)",
            ga4_pages["rows"][: options["top"]],
            reports.get(GA4Report.REPORT_PAGE),
            {row["page_path"]: row for row in dashboard["pages"]},
            ["views", "active_users"],
        )
        self.compare_daily(ga4_daily["rows"], start_date, end_date, dashboard)
        self.print_additivity_checks(ga4_summary, ga4_sources, ga4_pages, ga4_daily)

        if self.mismatches:
            raise CommandError(
                f"{self.mismatches} value(s) differ between GA4, "
                "database and dashboard."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\nAll compared values match GA4."
            )
        )

    def write_row(self, label, ga4_value, db_value, dashboard_value):
        matches = values_match(ga4_value, db_value, dashboard_value)

        if not matches:
            self.mismatches += 1

        line = (
            f"{label[:58]:<58} {format_value(ga4_value):>10} "
            f"{format_value(db_value):>10} "
            f"{format_value(dashboard_value):>10}  "
            f"{'OK' if matches else 'MISMATCH'}"
        )

        self.stdout.write(
            line if matches else self.style.ERROR(line)
        )

    def write_heading(self, title):
        self.stdout.write(f"\n{title}")
        self.stdout.write(
            f"{'Metric':<58} {'GA4':>10} {'DB':>10} {'Dashboard':>10}"
        )

    def compare_summary(self, ga4_summary, reports, dashboard):
        self.write_heading("SITE TOTALS")

        ga4_row = ga4_summary["rows"][0]
        summary_report = reports.get(GA4Report.REPORT_SUMMARY)
        db_row = summary_report.rows.first() if summary_report else None
        dashboard_summary = dashboard["summary"] or {}

        for label, field in SUMMARY_METRICS:
            self.write_row(
                label,
                ga4_row[field],
                getattr(db_row, field, None),
                dashboard_summary.get(field),
            )

        engagement = ga4_row["engagement_duration_seconds"]

        self.write_row(
            "Avg Engagement / Session (s)",
            ratio(engagement, ga4_row["sessions"]),
            getattr(db_row, "avg_engagement_time_per_session", None),
            dashboard_summary.get("avg_engagement_time_per_session"),
        )

        self.write_row(
            "Avg Engagement / Active User (s)",
            ratio(engagement, ga4_row["active_users"]),
            getattr(db_row, "avg_engagement_time_per_active_user", None),
            dashboard_summary.get("avg_engagement_time_per_active_user"),
        )

    def compare_dimension(
        self,
        title,
        ga4_rows,
        report,
        dashboard_rows,
        fields,
    ):
        self.write_heading(title)

        db_rows = (
            {row.dimension_value: row for row in report.rows.all()}
            if report
            else {}
        )

        for ga4_row in ga4_rows:
            key = ga4_row["dimension_value"]
            short_key = key if len(key) <= 40 else f"{key[:37]}..."

            for field in fields:
                self.write_row(
                    f"{short_key} [{field}]",
                    ga4_row[field],
                    getattr(db_rows.get(key), field, None),
                    dashboard_rows.get(key, {}).get(field),
                )

    def compare_daily(self, ga4_rows, start_date, end_date, dashboard):
        self.write_heading("DAILY TREND")

        db_rows = {
            row.metric_date: row
            for row in GA4DailyMetric.objects.filter(
                property_id=GA4_PROPERTY_ID,
                metric_date__range=(start_date, end_date),
            )
        }

        dashboard_rows = {
            row["date"]: row
            for row in dashboard["daily"]
        }

        for ga4_row in ga4_rows:
            day = ga4_row["metric_date"]

            for field in ["views", "sessions", "total_users"]:
                self.write_row(
                    f"{day} [{field}]",
                    ga4_row[field],
                    getattr(db_rows.get(day), field, None),
                    dashboard_rows.get(day, {}).get(field),
                )

    def print_additivity_checks(
        self,
        ga4_summary,
        ga4_sources,
        ga4_pages,
        ga4_daily,
    ):
        """
        Informational: shows why site totals must not be summed from
        dimensioned rows.
        """

        site = ga4_summary["rows"][0]

        def total(rows, field):
            return sum(row[field] or 0 for row in rows)

        self.stdout.write("\nADDITIVITY CHECKS (informational)")

        checks = [
            (
                "Sessions: sum over source / medium (additive)",
                total(ga4_sources["rows"], "sessions"),
                site["sessions"],
            ),
            (
                "Views: sum over pages (additive)",
                total(ga4_pages["rows"], "views"),
                site["views"],
            ),
            (
                "Active users: sum over pages (NOT additive)",
                total(ga4_pages["rows"], "active_users"),
                site["active_users"],
            ),
            (
                "Total users: sum over days (NOT additive)",
                total(ga4_daily["rows"], "total_users"),
                site["total_users"],
            ),
        ]

        for label, row_sum, site_total in checks:
            self.stdout.write(
                f"{label:<58} row sum {row_sum:>6}   site {site_total:>6}"
            )
