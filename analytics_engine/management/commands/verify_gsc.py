"""
Compare raw Search Console API values with the database and the Search
Console dashboard data for one date range.

Read-only unless --collect is given. Pass/fail compares the live API
(the authority) with the stored reports, the dashboard payload and the
rendered page, for the same property, search type, dataState and
aggregation. Every selected date is checked for its status (final,
preliminary or pending) and, when it has data, its values. An exported
Search Console folder (--export-dir) is shown as an archived reference:
differences from it are reported, never used to pass or fail.
"""

import csv
import html
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import unquote

from django.contrib.auth.models import AnonymousUser
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.test import RequestFactory

from analytics_engine import views
from analytics_engine.collectors.search_console_db_collector import (
    collect_search_console_range,
    report_identity,
)
from analytics_engine.collectors.search_console_reports import (
    GSC_SEARCH_TYPE,
    GSC_SITE_URL,
    GSC_TIMEZONE,
    create_search_console_service,
    data_through,
    fetch_daily_range,
    fetch_freshness,
    fetch_gsc_pages,
    fetch_gsc_queries,
    fetch_gsc_summary,
    dashboard_today,
    preliminary_through,
    report_data_state,
    validate_property_access,
)
from analytics_engine.models import (
    SearchConsoleDailyMetric,
    SearchConsoleReport,
)
from analytics_engine.services.search_console_dashboard import (
    build_search_console_payload,
    get_range_reports,
    is_range_current,
)


PENDING = "pending"


def ctr_percent(clicks, impressions):
    if not impressions or clicks is None:
        return None
    return clicks / impressions * 100


def round_half_up(value, places=2):
    """Presentation rounding, computed independently of the template."""

    return Decimal(repr(value)).quantize(
        Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
    )


def weighted_position(rows):
    """sum(position * impressions) / sum(impressions)."""

    impressions = sum(row["impressions"] for row in rows)

    if not impressions:
        return None

    return (
        sum(row["position"] * row["impressions"] for row in rows)
        / impressions
    )


def format_value(value, decimals=4):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return str(value)


def normalize(value):
    return round(value, 6) if isinstance(value, float) else value


def values_match(*values):
    return len({normalize(value) for value in values}) == 1


def metric_rows(row):
    """(label, value) pairs for one metrics dict or model row."""

    def get(name):
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(name)
        return getattr(row, name)

    clicks = get("clicks")
    impressions = get("impressions")

    return [
        ("Clicks", clicks),
        ("Impressions", impressions),
        ("CTR %", ctr_percent(clicks, impressions)),
        ("Position", get("position")),
    ]


# ---------------------------------------------------------------------------
# Exported Search Console folder (Chart.csv, Queries.csv, Pages.csv, ...)
# ---------------------------------------------------------------------------


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def parse_export_number(value):
    value = value.strip().rstrip("%")
    decimals = len(value.split(".")[1]) if "." in value else 0
    return float(value), decimals


def parse_export_date(value):
    value = value.strip().replace("Sept", "Sep")
    return datetime.strptime(value, "%d %b %Y").date()


def load_export(export_dir):
    export_dir = Path(export_dir)

    export = {"dir": export_dir, "range": None, "filters": {}}

    filters_path = export_dir / "Filters.csv"

    if filters_path.exists():
        for row in read_csv(filters_path)[1:]:
            if len(row) >= 2:
                export["filters"][row[0].strip()] = row[1].strip()

        date_filter = export["filters"].get("Date", "")

        try:
            start_text, end_text = date_filter.split("-")
            export["range"] = (
                parse_export_date(start_text),
                parse_export_date(end_text),
            )
        except ValueError:
            export["range"] = None

    def metric_table(file_name):
        path = export_dir / file_name

        if not path.exists():
            return None

        table = {}

        for row in read_csv(path)[1:]:
            if len(row) < 5:
                continue

            table[row[0].strip()] = {
                "clicks": int(row[1]),
                "impressions": int(row[2]),
                "ctr_percent": parse_export_number(row[3]),
                "position": parse_export_number(row[4]),
            }

        return table

    chart = metric_table("Chart.csv")

    export["chart"] = (
        {date.fromisoformat(key): value for key, value in chart.items()}
        if chart
        else None
    )
    export["queries"] = metric_table("Queries.csv")
    export["pages"] = metric_table("Pages.csv")

    return export


def export_key(value):
    """
    Search Console's CSV export wraps long queries/URLs with line breaks
    and decodes %20 in URLs; compare keys without those artifacts.
    """

    return " ".join(unquote(value).split())


def export_matches(api_value, export_entry):
    """Compare an API value with an export value at the export's precision."""

    if export_entry is None or api_value is None:
        return export_entry is None and api_value is None

    if isinstance(export_entry, tuple):
        export_value, decimals = export_entry
        return abs(api_value - export_value) <= 0.5 * 10 ** -decimals + 1e-9

    return api_value == export_entry


def export_display(export_entry):
    if export_entry is None:
        return "-"
    if isinstance(export_entry, tuple):
        value, decimals = export_entry
        return f"{value:.{decimals}f}"
    return str(export_entry)


class Command(BaseCommand):
    help = (
        "Compare raw Search Console API values with the database and "
        "the Search Console dashboard data for one date range."
    )

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="YYYY-MM-DD")
        parser.add_argument("--end", required=True, help="YYYY-MM-DD")

        parser.add_argument(
            "--collect",
            action="store_true",
            help=(
                "Collect this range from Search Console into the "
                "database first. Without it, nothing is written."
            ),
        )

        parser.add_argument(
            "--top",
            type=int,
            default=10,
            help="Number of queries and pages to compare (default 10).",
        )

        parser.add_argument(
            "--export-dir",
            help=(
                "Folder of a Search Console Performance export "
                "(Chart.csv, Queries.csv, Pages.csv, Filters.csv) to "
                "show as an archived reference."
            ),
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
        self.export_differences = 0

        export = (
            load_export(options["export_dir"])
            if options["export_dir"]
            else None
        )

        service = create_search_console_service()
        permission_level = validate_property_access(service)

        if options["collect"]:
            result = collect_search_console_range(
                start_date,
                end_date,
                service=service,
            )
            self.stdout.write(
                f"Collected range: {result['rows_written']} DB rows "
                "written\n"
            )

        freshness = fetch_freshness(service, dashboard_today())
        data_state = report_data_state(end_date, freshness)

        # What Google returns now for the range, in the dataState the
        # dashboard must use for it.
        self.expected = {
            "data_state": data_state,
            "final_through": data_through(
                start_date,
                end_date,
                freshness["latest_final_date"],
            ),
            "preliminary_through": preliminary_through(
                start_date,
                end_date,
                freshness,
                data_state,
            ),
        }
        self.expected["covered_through"] = (
            self.expected["preliminary_through"]
            or self.expected["final_through"]
        )

        api_summary = fetch_gsc_summary(
            service, start_date, end_date, data_state=data_state
        )
        api_daily = fetch_daily_range(
            service, start_date, end_date, freshness
        )
        api_queries = fetch_gsc_queries(
            service, start_date, end_date, data_state=data_state
        )
        api_pages = fetch_gsc_pages(
            service, start_date, end_date, data_state=data_state
        )
        latest_final = freshness["latest_final_date"]

        reports = get_range_reports(start_date, end_date)
        dashboard = build_search_console_payload(start_date, end_date)

        self.write_header(
            permission_level,
            start_date,
            end_date,
            latest_final,
            api_summary,
            reports,
            dashboard,
            export,
        )

        self.compare_summary(api_summary, reports, dashboard, export,
                             start_date, end_date)
        self.compare_daily(api_daily, start_date, end_date, dashboard,
                           export)
        self.compare_kpis_with_chart(dashboard)
        self.compare_dimension(
            "QUERIES",
            api_queries,
            reports.get(SearchConsoleReport.REPORT_QUERY),
            dashboard["queries"],
            "query",
            options["top"],
            export["queries"] if self.export_covers(export, start_date,
                                                    end_date) else None,
        )
        self.compare_dimension(
            "PAGES",
            api_pages,
            reports.get(SearchConsoleReport.REPORT_PAGE),
            dashboard["pages"],
            "page_url",
            options["top"],
            export["pages"] if self.export_covers(export, start_date,
                                                  end_date) else None,
        )
        self.compare_rendered(
            start_date,
            end_date,
            api_summary,
            api_daily,
            api_queries,
            api_pages,
        )
        self.print_page_titles(dashboard, options["top"])
        self.print_aggregation_checks(
            api_summary,
            api_daily,
            api_queries,
            api_pages,
        )

        if self.export_differences:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{self.export_differences} value(s) differ from "
                    "the archived export (not used for pass/fail; check "
                    "for Google data revisions or different filters)."
                )
            )

        if self.mismatches:
            raise CommandError(
                f"{self.mismatches} value(s) differ between the Search "
                "Console API, database and dashboard."
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\nPASS: all compared database and dashboard values "
                "match the Search Console API."
            )
        )

    # -- output helpers ---------------------------------------------------

    def export_covers(self, export, start_date, end_date):
        return bool(export and export["range"] == (start_date, end_date))

    def write_heading(self, title, with_export=False):
        self.stdout.write(f"\n{title}")
        self.stdout.write(
            f"{'Metric':<52} "
            + (f"{'Export':>10} " if with_export else "")
            + f"{'GSC API':>12} {'DB':>12} {'Dashboard':>12}"
        )

    def write_row(
        self,
        label,
        api_value,
        db_value,
        dashboard_value,
        export_entry=None,
        with_export=False,
        compare_export=True,
    ):
        matches = values_match(api_value, db_value, dashboard_value)

        if not matches:
            self.mismatches += 1

        export_note = ""

        if with_export and compare_export and export_entry is not None:
            if not export_matches(api_value, export_entry):
                self.export_differences += 1
                export_note = "  EXPORT DIFF"

        line = (
            f"{label[:52]:<52} "
            + (
                f"{export_display(export_entry):>10} "
                if with_export
                else ""
            )
            + f"{format_value(api_value):>12} "
            f"{format_value(db_value):>12} "
            f"{format_value(dashboard_value):>12}  "
            f"{'OK' if matches else 'MISMATCH'}{export_note}"
        )

        self.stdout.write(line if matches else self.style.ERROR(line))

    def write_header(
        self,
        permission_level,
        start_date,
        end_date,
        latest_final,
        api_summary,
        reports,
        dashboard,
        export,
    ):
        summary_report = reports.get(SearchConsoleReport.REPORT_SUMMARY)
        period = dashboard["period"]
        expected = self.expected

        self.stdout.write(f"PROPERTY:          {GSC_SITE_URL} "
                          f"({permission_level})")
        self.stdout.write(f"SEARCH TYPE:       {GSC_SEARCH_TYPE}")
        self.stdout.write(
            f"AGGREGATION:       summary/daily/query byProperty, "
            f"page byPage (API summary reported "
            f"{api_summary['aggregation_type'] or '-'})"
        )
        self.stdout.write(
            f"DATE RANGE:        {start_date} to {end_date} "
            f"({(end_date - start_date).days + 1} days, inclusive, "
            f"{GSC_TIMEZONE} dates)"
        )
        self.stdout.write(
            f"GOOGLE NOW:        final through {latest_final}, "
            f"preliminary through {expected['preliminary_through'] or '-'} "
            f"-> dataState {expected['data_state']}"
        )
        self.stdout.write(
            "DB REPORT:         "
            + (
                f"collected {summary_report.collected_at:%Y-%m-%d %H:%M} "
                f"UTC, dataState {summary_report.data_state}, final "
                f"through {summary_report.data_through}, preliminary "
                f"through {summary_report.preliminary_through or '-'}"
                if summary_report
                else "not stored (re-run with --collect)"
            )
        )
        self.stdout.write(
            f"DASHBOARD:         final through {period['data_through']}, "
            f"preliminary {period['preliminary_start'] or '-'} to "
            f"{period['preliminary_end'] or '-'}, pending "
            f"{period['pending_start'] or '-'} to "
            f"{period['pending_end'] or '-'}"
        )

        if export:
            self.stdout.write(
                f"EXPORT:            {export['dir']} "
                f"(filters: {export['filters']})"
            )

            if export["filters"].get("Search type", "Web") != "Web":
                self.stdout.write(
                    self.style.WARNING(
                        "Export search type is not Web - export "
                        "values are not comparable."
                    )
                )

        self.write_heading("REPORTING MODE")
        self.write_row(
            "dataState (summary, queries, pages)",
            expected["data_state"],
            summary_report.data_state if summary_report else None,
            period["data_state"],
        )
        self.write_row(
            "Finalized through",
            expected["final_through"],
            summary_report.data_through if summary_report else None,
            period["data_through"],
        )
        self.write_row(
            "Preliminary through",
            expected["preliminary_through"],
            summary_report.preliminary_through if summary_report else None,
            period["preliminary_end"],
        )

    def compare_summary(self, api_summary, reports, dashboard, export,
                        start_date, end_date):
        chart = export["chart"] if export else None
        in_export = bool(chart) and all(
            start_date + timedelta(days=offset) in chart
            for offset in range((end_date - start_date).days + 1)
        )

        export_totals = None

        if in_export:
            days = [
                chart[start_date + timedelta(days=offset)]
                for offset in range((end_date - start_date).days + 1)
            ]
            clicks = sum(day["clicks"] for day in days)
            impressions = sum(day["impressions"] for day in days)
            export_totals = {
                "Clicks": clicks,
                "Impressions": impressions,
                "CTR %": (
                    (round(ctr_percent(clicks, impressions), 2), 2)
                    if impressions
                    else None
                ),
                # Approximate: weighted from rounded daily positions.
                "Position": (
                    (
                        weighted_position(
                            [
                                {
                                    "position": day["position"][0],
                                    "impressions": day["impressions"],
                                }
                                for day in days
                            ]
                        ),
                        3,
                    )
                    if impressions
                    else None
                ),
            }

        self.write_heading("PROPERTY SUMMARY", with_export=in_export)

        summary_report = reports.get(SearchConsoleReport.REPORT_SUMMARY)
        db_row = summary_report.rows.first() if summary_report else None

        for (label, api_value), (_, db_value), (_, dashboard_value) in zip(
            metric_rows(api_summary["rows"][0]),
            metric_rows(db_row),
            metric_rows(dashboard["summary"]),
        ):
            self.write_row(
                label,
                api_value,
                db_value,
                dashboard_value,
                export_entry=(
                    export_totals.get(label) if export_totals else None
                ),
                with_export=in_export,
                compare_export=label != "Position",
            )

        if in_export:
            self.stdout.write(
                "  Export position is ~approximate (weighted from "
                "rounded daily values); the API value is authoritative."
            )

    def compare_daily(self, api_daily, start_date, end_date, dashboard,
                      export):
        """
        Every selected date: its status, then (unless pending) its
        values. A covered date Google omits had no impressions (zero);
        dates after Google's latest data are pending (no values).
        """

        chart = export["chart"] if export else None
        with_export = bool(chart)

        self.write_heading("DAILY (byProperty, date dimension)",
                           with_export=with_export)

        api_rows = {row["metric_date"]: row for row in api_daily["rows"]}

        db_rows = {
            row.metric_date: row
            for row in SearchConsoleDailyMetric.objects.filter(
                **report_identity(),
                metric_date__range=(start_date, end_date),
            )
        }

        dashboard_rows = {row["date"]: row for row in dashboard["daily"]}

        final_through = self.expected["final_through"]
        covered_through = self.expected["covered_through"]

        zero = {"clicks": 0, "impressions": 0, "ctr": None, "position": None}

        day = start_date

        while day <= end_date:
            api_row = api_rows.get(day)
            db_row = db_rows.get(day)
            dashboard_row = dashboard_rows.get(day) or {}

            if covered_through is None or day > covered_through:
                api_status = PENDING
            elif api_row:
                api_status = api_row["data_state"]
            elif final_through is not None and day <= final_through:
                api_status = "final"
            else:
                api_status = "preliminary"

            if api_status == PENDING:
                db_status = PENDING if db_row is None else db_row.data_state
            elif db_row is not None:
                db_status = db_row.data_state
            else:
                # No stored row is correct only when Google has none.
                db_status = api_status if api_row is None else "missing"

            self.write_row(
                f"{day} [status]",
                api_status,
                db_status,
                dashboard_row.get("status", "-"),
            )

            if api_status == PENDING:
                day += timedelta(days=1)
                continue

            api_values = api_row or zero
            db_values = db_row if db_row else (zero if not api_row else None)

            export_day = chart.get(day) if chart else None

            for (label, api_value), (_, db_value), (_, dashboard_value) in zip(
                metric_rows(api_values),
                metric_rows(db_values),
                metric_rows(dashboard_row),
            ):
                export_key = {
                    "Clicks": "clicks",
                    "Impressions": "impressions",
                    "CTR %": "ctr_percent",
                    "Position": "position",
                }[label]

                self.write_row(
                    f"{day} [{label}]",
                    api_value,
                    db_value,
                    dashboard_value,
                    export_entry=(
                        export_day[export_key] if export_day else None
                    ),
                    with_export=with_export,
                    compare_export=api_status == "final",
                )

            day += timedelta(days=1)

    def compare_kpis_with_chart(self, dashboard):
        """
        The KPI cards and the chart must cover the same dates: property
        clicks and impressions are additive across dates.
        """

        self.write_heading("KPI CARDS vs DAILY CHART (dashboard)")

        summary = dashboard["summary"] or {}
        days = [day for day in dashboard["daily"] if day["status"] != PENDING]

        for field in ("clicks", "impressions"):
            chart_total = sum(day[field] for day in days)
            self.write_row(
                f"{field}: KPI = sum of {len(days)} chart dates",
                summary.get(field),
                chart_total,
                chart_total,
            )

    def compare_dimension(self, title, api_report, report, dashboard_list,
                          key_field, top, export_table):
        """
        Every row Google returns for the dimension, compared with the
        stored report and the dashboard list: same keys, no duplicates,
        same clicks / impressions / CTR / position, same sort order.
        The first `top` rows are printed; any mismatching row is
        printed wherever it is.
        """

        with_export = export_table is not None
        api_rows = api_report["rows"]

        self.write_heading(
            f"{title}: ALL {len(api_rows)} API ROWS "
            f"(aggregation {api_report['aggregation_type'] or '-'}, "
            f"dataState {api_report['data_state']})",
        )

        stored = list(report.rows.all()) if report else []
        db_rows = {row.dimension_value: row for row in stored}
        dashboard_rows = {row[key_field]: row for row in dashboard_list}
        api_keys = [row["dimension_value"] for row in api_rows]

        self.write_row(
            "Row count",
            len(api_rows),
            len(stored) if report else None,
            len(dashboard_list),
        )
        self.write_row(
            "Distinct keys (no duplicates)",
            len(set(api_keys)),
            len(db_rows) if report else None,
            len(dashboard_rows),
        )
        self.write_row(
            "Keys missing vs API",
            0,
            len(set(api_keys) - set(db_rows)) if report else None,
            len(set(api_keys) - set(dashboard_rows)),
        )
        self.write_row(
            "Keys not returned by API",
            0,
            len(set(db_rows) - set(api_keys)) if report else None,
            len(set(dashboard_rows) - set(api_keys)),
        )

        # Dashboard order: clicks desc, impressions desc, then key.
        expected_order = [
            row["dimension_value"]
            for row in sorted(
                api_rows,
                key=lambda row: (
                    -row["clicks"],
                    -row["impressions"],
                    row["dimension_value"],
                ),
            )
        ]
        self.write_row(
            "Sort order (clicks, impressions, key)",
            "sorted",
            "sorted",
            "sorted"
            if [row[key_field] for row in dashboard_list] == expected_order
            else "out of order",
        )

        fields = [
            ("clicks", "clicks"),
            ("impressions", "impressions"),
            ("CTR %", "ctr_percent"),
            ("position", "position"),
        ]

        mismatched_rows = 0

        for index, api_row in enumerate(api_rows):
            key = api_row["dimension_value"]
            db_row = db_rows.get(key)
            dashboard_row = dashboard_rows.get(key, {})
            export_row = export_table.get(key) if export_table else None

            api_values = {
                "clicks": api_row["clicks"],
                "impressions": api_row["impressions"],
                "ctr_percent": ctr_percent(
                    api_row["clicks"], api_row["impressions"]
                ),
                "position": api_row["position"],
            }
            db_values = (
                {
                    "clicks": db_row.clicks,
                    "impressions": db_row.impressions,
                    "ctr_percent": db_row.ctr_percent,
                    "position": db_row.position,
                }
                if db_row
                else {}
            )

            row_matches = all(
                values_match(
                    api_values[field],
                    db_values.get(field),
                    dashboard_row.get(field),
                )
                for _, field in fields
            )

            if not row_matches:
                mismatched_rows += 1

            if index >= top and row_matches:
                continue

            short_key = key if len(key) <= 36 else f"...{key[-33:]}"

            for label, field in fields:
                self.write_row(
                    f"{short_key} [{label}]",
                    api_values[field],
                    db_values.get(field),
                    dashboard_row.get(field),
                    export_entry=(
                        export_row.get(field) if export_row else None
                    ),
                    with_export=with_export,
                )

        # Every mismatching row was printed (and counted) above.
        self.stdout.write(
            f"  {len(api_rows)} rows x {len(fields)} metrics = "
            f"{len(api_rows) * len(fields)} values compared; "
            f"{mismatched_rows} row(s) differ (first "
            f"{min(top, len(api_rows))} rows shown)"
        )

        if export_table is not None:
            by_export_key = {
                export_key(key): row for key, row in dashboard_rows.items()
            }
            found = 0
            differing = []

            for key, export_row in export_table.items():
                row = by_export_key.get(export_key(key))

                if row is None:
                    continue

                found += 1

                if (export_row["clicks"], export_row["impressions"]) != (
                    row["clicks"],
                    row["impressions"],
                ):
                    differing.append((key, export_row, row))

            self.stdout.write(
                f"  Export: {len(export_table)} rows, {found} found on the "
                f"dashboard, {len(differing)} with different "
                "clicks/impressions"
            )

            for key, export_row, row in differing:
                self.stdout.write(
                    self.style.WARNING(
                        f"    EXPORT DIFF {export_key(key)[-60:]}: export "
                        f"{export_row['clicks']}/{export_row['impressions']}"
                        f", now {row['clicks']}/{row['impressions']}"
                    )
                )

            self.export_differences += len(differing)


    def compare_rendered(self, start_date, end_date, api_summary,
                         api_daily, api_queries, api_pages):
        """
        Render the Search Console page for the range and compare what a
        viewer sees with the API: KPI cards (presentation-rounded),
        freshness line and daily chart data (null = awaiting Google).
        """

        self.stdout.write(
            f"\nRENDERED PAGE (/search-console/?start={start_date}"
            f"&end={end_date})"
        )

        # Rendering a range that is not stored would fetch it from the
        # API and write it, which the default read-only mode must not do.
        if not is_range_current(start_date, end_date):
            self.mismatches += 1
            self.stdout.write(
                self.style.ERROR(
                    "Range is not stored or not current: not rendered "
                    "(re-run with --collect).  MISMATCH"
                )
            )
            return

        request = RequestFactory().get(
            "/search-console/",
            {"start": start_date.isoformat(), "end": end_date.isoformat()},
        )
        request.user = AnonymousUser()

        page = views.search_console.__wrapped__(request).content.decode()

        site = api_summary["rows"][0]
        ctr = ctr_percent(site["clicks"], site["impressions"])

        expected_kpis = {
            "clicks": str(site["clicks"]),
            "impressions": str(site["impressions"]),
            "ctr": f"{round_half_up(ctr)}%" if ctr is not None else "—",
            "position": (
                str(round_half_up(site["position"]))
                if site["position"] is not None
                else "—"
            ),
        }

        self.stdout.write(f"{'Element':<52} {'Expected':>14} {'Rendered':>14}")

        for name, expected in expected_kpis.items():
            match = re.search(
                rf'data-kpi="{name}">\s*(.*?)\s*</strong>', page, re.S
            )
            self.write_check(
                f"KPI card [{name}]",
                expected,
                match.group(1) if match else None,
            )

        covered_through = self.expected["covered_through"]

        freshness = re.search(
            r"data-freshness>(.*?)</p>", page, re.S
        )
        freshness_text = (
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", freshness.group(1)))
            .strip()
            if freshness
            else ""
        )
        expected_freshness = (
            f"Data available through {covered_through.day} "
            f"{covered_through:%b}"
            if covered_through
            else "Awaiting Google data"
        )
        self.write_check(
            "Freshness line",
            expected_freshness,
            expected_freshness
            if expected_freshness in freshness_text
            else freshness_text,
        )

        api_rows = {row["metric_date"]: row for row in api_daily["rows"]}

        for field, element_id in [
            ("clicks", "sc-daily-clicks"),
            ("impressions", "sc-daily-impressions"),
        ]:
            expected = []
            day = start_date

            while day <= end_date:
                if covered_through and day <= covered_through:
                    row = api_rows.get(day)
                    expected.append(row[field] if row else 0)
                else:
                    expected.append(None)
                day += timedelta(days=1)

            match = re.search(
                rf'id="{element_id}"[^>]*>(.*?)</script>', page, re.S
            )
            rendered = json.loads(match.group(1)) if match else None

            self.write_check(
                f"Daily chart [{field}] "
                f"({sum(v is not None for v in expected)} values, "
                f"{sum(v is None for v in expected)} gaps)",
                "match API",
                "match API" if rendered == expected else str(rendered)[:40],
            )

        final_through = self.expected["final_through"]
        expected_status = []
        day = start_date

        while day <= end_date:
            row = api_rows.get(day)

            if not covered_through or day > covered_through:
                expected_status.append(PENDING)
            elif row:
                expected_status.append(row["data_state"])
            elif final_through and day <= final_through:
                expected_status.append("final")
            else:
                expected_status.append("preliminary")
            day += timedelta(days=1)

        match = re.search(
            r'id="sc-daily-status"[^>]*>(.*?)</script>', page, re.S
        )
        rendered = json.loads(match.group(1)) if match else None

        self.write_check(
            f"Daily chart status ({expected_status.count('final')} final, "
            f"{expected_status.count('preliminary')} prelim., "
            f"{expected_status.count(PENDING)} pending)",
            "match API",
            "match API"
            if rendered == expected_status
            else str(rendered)[:40],
        )

        self.compare_rendered_tables(page, api_queries, api_pages)

    def compare_rendered_tables(self, page, api_queries, api_pages):
        """
        The Top Queries chart and the Query Performance table must show
        the same query dataset, and the Page Performance table the page
        dataset, in the same order as Google's rows.
        """

        queries = [row["dimension_value"] for row in api_queries["rows"]]
        clicks = [row["clicks"] for row in api_queries["rows"]]

        def script_json(element_id):
            match = re.search(
                rf'id="{element_id}"[^>]*>(.*?)</script>', page, re.S
            )
            return json.loads(match.group(1)) if match else None

        self.write_check(
            "Top Queries chart = first 10 query rows",
            "match API",
            "match API"
            if script_json("sc-query-labels") == queries[:10]
            and script_json("sc-query-clicks") == clicks[:10]
            else "differs",
        )

        def cells(css_class):
            return [
                html.unescape(re.sub(r"\s+", " ", cell).strip())
                for cell in re.findall(
                    rf'<td class="{css_class}">(.*?)</td>', page, re.S
                )
            ]

        rendered_queries = cells("sc-query-cell")
        self.write_check(
            f"Query table = first {len(rendered_queries)} query rows",
            "match API",
            "match API"
            if rendered_queries
            == [re.sub(r"\s+", " ", q).strip() for q in queries[:25]]
            else "differs",
        )

        rendered_urls = [
            html.unescape(url)
            for url in re.findall(
                r'<td class="sc-page-cell">.*?href="([^"]*)"', page, re.S
            )
        ]
        page_urls = [row["dimension_value"] for row in api_pages["rows"]]
        self.write_check(
            f"Page table = first {len(rendered_urls)} page rows",
            "match API",
            "match API" if rendered_urls == page_urls[:25] else "differs",
        )

    def write_check(self, label, expected, rendered):
        matches = expected == rendered

        if not matches:
            self.mismatches += 1

        line = (
            f"{label[:52]:<52} {str(expected)[:40]:>14} "
            f"{str(rendered)[:40]:>14}  {'OK' if matches else 'MISMATCH'}"
        )
        self.stdout.write(line if matches else self.style.ERROR(line))

    def print_page_titles(self, dashboard, top):
        self.stdout.write("\nPAGE TITLES (dashboard, informational)")

        for page in dashboard["pages"][:top]:
            title = page["page_title"] or "(no reliable title; URL shown)"
            self.stdout.write(f"  {page['page_url']}\n      -> {title}")

    def print_aggregation_checks(self, api_summary, api_daily, api_queries,
                                 api_pages):
        """
        Informational: why headline KPIs must come from the property
        summary rather than from query or page rows.
        """

        site = api_summary["rows"][0]

        def totals(rows):
            return (
                sum(row["clicks"] for row in rows),
                sum(row["impressions"] for row in rows),
            )

        self.stdout.write("\nAGGREGATION CHECKS (informational, raw API)")
        self.stdout.write(
            f"{'':<52} {'clicks':>8} {'impr.':>8}"
        )
        self.stdout.write(
            f"{'Property summary (byProperty)':<52} "
            f"{site['clicks']:>8} {site['impressions']:>8}"
        )

        for label, rows in [
            ("Sum of daily rows (byProperty, additive)", api_daily["rows"]),
            ("Sum of query rows (anonymized queries omitted)",
             api_queries["rows"]),
            ("Sum of page rows (byPage, impressions per page)",
             api_pages["rows"]),
        ]:
            clicks, impressions = totals(rows)
            self.stdout.write(
                f"{label:<52} {clicks:>8} {impressions:>8}"
            )

        daily_position = weighted_position(
            [row for row in api_daily["rows"] if row["impressions"]]
        )

        self.stdout.write(
            f"{'Impression-weighted daily position':<52} "
            f"{format_value(daily_position):>8}   "
            f"property {format_value(site['position'])}"
        )
