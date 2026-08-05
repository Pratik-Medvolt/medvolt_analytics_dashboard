from time import perf_counter

from django.core.management.base import (
    BaseCommand,
    CommandError,
)

from analytics_engine.collectors.content_discovery_db import (
    discover_content_to_database,
)
from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_to_database,
)
from analytics_engine.collectors.mailerlite_db_collector import (
    collect_mailerlite_to_database,
)
from analytics_engine.collectors.search_console_db_collector import (
    collect_search_console_to_database,
)
from analytics_engine.services.weekly_report_service import (
    generate_weekly_report,
)


class Command(BaseCommand):
    help = (
        "Run content discovery, collect analytics"
        "and generate the weekly report."
    )

    def handle(self, *args, **options):
        pipeline_started = perf_counter()

        results = {}
        errors = []

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\nStarting Medvolt analytics pipeline...\n"
            )
        )

        pipeline_steps = [
            (
                "content_discovery",
                "Content Discovery",
                discover_content_to_database,
            ),
            (
                "mailerlite",
                "MailerLite Analytics",
                collect_mailerlite_to_database,
            ),
            (
                "ga4",
                "GA4 Website Analytics",
                collect_ga4_to_database,
            ),
            (
                "search_console",
                "Search Console Analytics",
                collect_search_console_to_database,
            ),
            (
                "weekly_report",
                "Weekly Report",
                generate_weekly_report,
            ),
        ]

        for key, label, collector_function in pipeline_steps:
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"\nRunning: {label}"
                )
            )

            step_started = perf_counter()

            try:
                result = collector_function() or {}

                results[key] = {
                    "result": result,
                    "duration": (
                        perf_counter()
                        - step_started
                    ),
                    "status": "success",
                }

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Completed: {label}"
                    )
                )

            except Exception as error:
                duration = (
                    perf_counter()
                    - step_started
                )

                results[key] = {
                    "result": {},
                    "duration": duration,
                    "status": "failed",
                    "error": str(error),
                }

                errors.append(
                    f"{label}: {error}"
                )

                self.stderr.write(
                    self.style.ERROR(
                        f"Failed: {label}\n"
                        f"{error}"
                    )
                )

        total_duration = (
            perf_counter()
            - pipeline_started
        )

        self.print_final_summary(
            results=results,
            total_duration=total_duration,
            errors=errors,
        )

        if errors:
            raise CommandError(
                "Analytics pipeline completed "
                "with one or more failed collectors."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\nAnalytics pipeline completed "
                "successfully."
            )
        )

    def print_final_summary(
        self,
        results,
        total_duration,
        errors,
    ):
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n"
                + "=" * 58
                + "\nFINAL ANALYTICS PIPELINE SUMMARY"
                + "\n"
                + "=" * 58
            )
        )

        discovery = results.get(
            "content_discovery",
            {},
        )

        discovery_result = discovery.get(
            "result",
            {},
        )

        website_result = discovery_result.get(
            "website",
            {},
        )

        mailerlite_discovery = discovery_result.get(
            "mailerlite",
            {},
        )

        self.stdout.write(
            "\nCONTENT DISCOVERY"
        )

        self.stdout.write(
            f"Website found: "
            f"{website_result.get('found', 0)}"
        )

        self.stdout.write(
            f"Website created: "
            f"{website_result.get('created', 0)}"
        )

        self.stdout.write(
            f"Website updated: "
            f"{website_result.get('updated', 0)}"
        )

        self.stdout.write(
            f"Website failed: "
            f"{website_result.get('failed', 0)}"
        )

        self.stdout.write(
            f"MailerLite found: "
            f"{mailerlite_discovery.get('found', 0)}"
        )

        self.stdout.write(
            f"MailerLite created: "
            f"{mailerlite_discovery.get('created', 0)}"
        )

        self.stdout.write(
            f"MailerLite updated: "
            f"{mailerlite_discovery.get('updated', 0)}"
        )

        self.stdout.write(
            f"MailerLite failed: "
            f"{mailerlite_discovery.get('failed', 0)}"
        )

        self.stdout.write(
            f"Duration: "
            f"{discovery.get('duration', 0):.2f}s"
        )

        self.print_collector_summary(
            heading="MAILERLITE ANALYTICS",
            collector=results.get(
                "mailerlite",
                {},
            ),
        )

        self.print_collector_summary(
            heading="GA4 WEBSITE ANALYTICS",
            collector=results.get(
                "ga4",
                {},
            ),
            include_period=True,
        )

        self.print_collector_summary(
            heading="SEARCH CONSOLE ANALYTICS",
            collector=results.get(
                "search_console",
                {},
            ),
            include_period=True,
            include_property=True,
        )

        total_processed = 0
        total_failed = 0

        for key in [
            "mailerlite",
            "ga4",
            "search_console",
        ]:
            collector_result = (
                results.get(key, {})
                .get("result", {})
            )

            total_processed += int(
                collector_result.get(
                    "processed",
                    0,
                )
            )

            total_failed += int(
                collector_result.get(
                    "failed",
                    0,
                )
            )

        self.stdout.write(
            "\nOVERALL"
        )

        self.stdout.write(
            f"Analytics records processed: "
            f"{total_processed}"
        )

        self.stdout.write(
            f"Analytics rows failed: "
            f"{total_failed}"
        )

        self.stdout.write(
            f"Collector failures: "
            f"{len(errors)}"
        )

        self.stdout.write(
            f"Total duration: "
            f"{total_duration:.2f}s"
        )

        final_status = (
            "FAILED"
            if errors
            else "SUCCESS"
        )

        self.stdout.write(
            f"Pipeline status: {final_status}"
        )

        if errors:
            self.stdout.write(
                "\nERRORS"
            )

            for error in errors:
                self.stdout.write(
                    f"- {error}"
                )

        self.stdout.write(
            "=" * 58
        )




        weekly_report = results.get(
            "weekly_report",
            {},
        )

        weekly_result = weekly_report.get(
            "result",
            {},
        )

        self.stdout.write(
            "\nWEEKLY REPORT"
        )

        if weekly_result:
            report_action = (
                "Created"
                if weekly_result.get("created")
                else "Updated"
            )

            self.stdout.write(
                f"Action: {report_action}"
            )

            self.stdout.write(
                f"Period: "
                f"{weekly_result.get('week_start', '-')} "
                f"to "
                f"{weekly_result.get('week_end', '-')}"
            )

            self.stdout.write(
                f"Website views: "
                f"{weekly_result.get('website_views', 0)}"
            )

            self.stdout.write(
                f"Website sessions: "
                f"{weekly_result.get('website_sessions', 0)}"
            )

            self.stdout.write(
                f"Search clicks: "
                f"{weekly_result.get('search_clicks', 0)}"
            )

            self.stdout.write(
                f"Search impressions: "
                f"{weekly_result.get('search_impressions', 0)}"
            )

            self.stdout.write(
                f"Email opens: "
                f"{weekly_result.get('email_opens', 0)}"
            )

            self.stdout.write(
                f"Email clicks: "
                f"{weekly_result.get('email_clicks', 0)}"
            )
        else:
            self.stdout.write(
        "No weekly report result available."
            )

        self.stdout.write(
            f"Duration: "
            f"{weekly_report.get('duration', 0):.2f}s"
        )

        self.stdout.write(
            f"Status: "
            f"{weekly_report.get('status', 'unknown')}"
        )









    def print_collector_summary(
        self,
        heading,
        collector,
        include_period=False,
        include_property=False,
    ):
        result = collector.get(
            "result",
            {},
        )

        self.stdout.write(
            f"\n{heading}"
        )

        if include_property:
            self.stdout.write(
                f"Property: "
                f"{result.get('property', '-')}"
            )

        if include_period:
            start_date = result.get(
                "start_date",
                "-",
            )

            end_date = result.get(
                "end_date",
                "-",
            )

            self.stdout.write(
                f"Period: "
                f"{start_date} to {end_date}"
            )

        self.stdout.write(
            f"Processed: "
            f"{result.get('processed', 0)}"
        )

        self.stdout.write(
            f"Created: "
            f"{result.get('created', 0)}"
        )

        self.stdout.write(
            f"Updated: "
            f"{result.get('updated', 0)}"
        )

        self.stdout.write(
            f"Failed: "
            f"{result.get('failed', 0)}"
        )

        self.stdout.write(
            f"Duration: "
            f"{collector.get('duration', 0):.2f}s"
        )

        self.stdout.write(
            f"Status: "
            f"{collector.get('status', 'unknown')}"
        )