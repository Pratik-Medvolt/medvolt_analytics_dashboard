"""
Collect GA4 reports into the database.

Each logical report (see ga4_reports.py) is stored separately:

- GA4DailyMetric: one row per property + date (upserted).
- GA4Report + GA4ReportRow: one report per property + report type +
  exact date range. Re-collecting a range replaces its rows, so repeated
  runs are idempotent and never accumulate duplicates.

Sessions and users are non-additive across page, source and date
dimensions. Never sum report rows to produce site totals - read the
"summary" report for the same range instead.
"""

from datetime import timedelta
from urllib.parse import urljoin

from decouple import config
from django.db import transaction
from django.utils import timezone

from analytics_engine.collectors import ga4_reports
from analytics_engine.collectors.ga4_reports import (
    GA4_PROPERTY_ID,
    RANGE_REPORT_FETCHERS,
    create_ga4_client,
    fetch_ga4_daily_metrics,
    get_property_today,
    preset_range,
)
from analytics_engine.models import (
    GA4DailyMetric,
    GA4Report,
    GA4ReportRow,
    SystemLog,
)
from analytics_engine.services.content_registry import (
    get_or_create_website_content,
)


WEBSITE_BASE_URL = config(
    "WEBSITE_BASE_URL",
    default="https://medvolt.ai",
).strip().rstrip("/")

GA4_LOOKBACK_DAYS = config(
    "GA4_LOOKBACK_DAYS",
    default=90,
    cast=int,
)

# Dashboard presets, matching GA4 "Last N days".
PRESET_PERIODS = {
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
}

# GA4 keeps processing recent data for up to ~48h, so previously
# collected custom ranges ending this recently are refreshed on every run.
RECENT_RANGE_REFRESH_DAYS = 3

METRIC_FIELD_NAMES = list(ga4_reports.METRIC_FIELDS.values())


def build_page_url(page_path):
    """Convert a GA4 page path into a full website URL."""

    if not page_path or page_path == "/":
        return f"{WEBSITE_BASE_URL}/"

    return urljoin(
        f"{WEBSITE_BASE_URL}/",
        page_path.lstrip("/"),
    )


def metric_values(row):
    return {
        field: row[field]
        for field in METRIC_FIELD_NAMES
        if field in row
    }


def link_page_content(page_path, page_title, content_cache):
    """
    Keep the content registry populated with GA4-seen pages
    (previous collector behaviour). Returns a Content or None.
    """

    if not page_path.startswith("/"):
        return None

    if page_path not in content_cache:
        try:
            content, _ = get_or_create_website_content(
                url=build_page_url(page_path),
                title=page_title,
                source="ga4_fallback",
            )
        except ValueError:
            content = None

        content_cache[page_path] = content

    return content_cache[page_path]


def store_range_report(
    report_type,
    start_date,
    end_date,
    fetched,
    period_key="custom",
    content_cache=None,
):
    """
    Upsert one range report and replace its rows.

    Must be called inside a transaction.
    """

    report, created = GA4Report.objects.get_or_create(
        property_id=GA4_PROPERTY_ID,
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        defaults={"period_key": period_key},
    )

    # A custom request for the same dates as a preset must not hide the
    # preset from the dashboard.
    if period_key != "custom":
        report.period_key = period_key

    report.property_timezone = fetched["property_timezone"]
    report.row_count = len(fetched["rows"])
    report.save()

    report.rows.all().delete()

    content_cache = (
        content_cache
        if content_cache is not None
        else {}
    )

    new_rows = []

    for row in fetched["rows"]:
        dimension_value = row.get("dimension_value", "")

        content = None

        if report_type == GA4Report.REPORT_PAGE:
            content = link_page_content(
                dimension_value,
                row.get("page_title", ""),
                content_cache,
            )

        new_rows.append(
            GA4ReportRow(
                report=report,
                dimension_value=dimension_value,
                page_title=row.get("page_title", "")[:500],
                content=content,
                **metric_values(row),
            )
        )

    GA4ReportRow.objects.bulk_create(new_rows)

    return report, created


def store_daily_metrics(fetched):
    """Upsert date-dimension rows. Must be called inside a transaction."""

    created_count = 0

    for row in fetched["rows"]:
        _, created = GA4DailyMetric.objects.update_or_create(
            property_id=GA4_PROPERTY_ID,
            metric_date=row["metric_date"],
            defaults=metric_values(row),
        )

        created_count += int(created)

    return created_count


def fetch_range_reports(client, start_date, end_date):
    return {
        report_type: fetcher(client, start_date, end_date)
        for report_type, fetcher in RANGE_REPORT_FETCHERS.items()
    }


def collect_ga4_range(
    start_date,
    end_date,
    period_key="custom",
    client=None,
    include_daily=True,
):
    """
    Fetch and store every range report for one exact date range.

    All GA4 requests run before the database is touched, so a failed
    request never leaves a partially refreshed range.
    """

    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    client = client or create_ga4_client()

    fetched_reports = fetch_range_reports(client, start_date, end_date)

    fetched_daily = (
        fetch_ga4_daily_metrics(client, start_date, end_date)
        if include_daily
        else None
    )

    rows_written = 0
    reports_created = 0

    with transaction.atomic():
        content_cache = {}

        for report_type, fetched in fetched_reports.items():
            _, created = store_range_report(
                report_type,
                start_date,
                end_date,
                fetched,
                period_key=period_key,
                content_cache=content_cache,
            )

            rows_written += len(fetched["rows"])
            reports_created += int(created)

        if fetched_daily:
            store_daily_metrics(fetched_daily)
            rows_written += len(fetched_daily["rows"])

    return {
        "start_date": start_date,
        "end_date": end_date,
        "period_key": period_key,
        "rows_written": rows_written,
        "reports_created": reports_created,
        "reports_updated": len(fetched_reports) - reports_created,
    }


def get_ranges_to_collect(today):
    """
    The ranges refreshed by a normal collector run, in the order they are
    stored (presets last so their period_key wins on identical dates).
    """

    # Imported here to avoid a circular import with the weekly service.
    from analytics_engine.services.weekly_report_service import (
        get_previous_completed_week,
    )

    ranges = []

    week_start, week_end = get_previous_completed_week(today)
    ranges.append((week_start, week_end, "week"))

    recent_cutoff = today - timedelta(days=RECENT_RANGE_REFRESH_DAYS)

    recent_custom = (
        GA4Report.objects.filter(
            property_id=GA4_PROPERTY_ID,
            report_type=GA4Report.REPORT_SUMMARY,
            period_key="custom",
            end_date__gte=recent_cutoff,
        )
        .values_list("start_date", "end_date")
        .distinct()
    )

    for start_date, end_date in recent_custom:
        ranges.append((start_date, end_date, "custom"))

    for period_key, days in PRESET_PERIODS.items():
        start_date, end_date = preset_range(days, today)
        ranges.append((start_date, end_date, period_key))

    # Drop duplicate date ranges, keeping the last (most specific) key.
    unique_ranges = {}

    for start_date, end_date, period_key in ranges:
        unique_ranges.pop((start_date, end_date), None)
        unique_ranges[(start_date, end_date)] = period_key

    return [
        (start_date, end_date, period_key)
        for (start_date, end_date), period_key in unique_ranges.items()
    ]


def collect_ga4_to_database(start_date=None, end_date=None):
    """
    Refresh GA4 data.

    Without arguments: daily metrics for the lookback window, plus range
    reports for the dashboard presets, the previous completed week and
    recently requested custom ranges.

    With start_date/end_date: that exact range only (plus its daily rows).
    """

    log = SystemLog.objects.create(
        module="ga4_collector",
        status="running",
        records_processed=0,
    )

    rows_written = 0
    created_count = 0
    updated_count = 0

    try:
        client = create_ga4_client()

        today, property_timezone = get_property_today(client)

        if start_date and end_date:
            ranges = [(start_date, end_date, "custom")]
            daily_start, daily_end = start_date, end_date

        else:
            ranges = get_ranges_to_collect(today)
            daily_start, daily_end = preset_range(GA4_LOOKBACK_DAYS, today)

        print(
            f"GA4 property {GA4_PROPERTY_ID} "
            f"(timezone {property_timezone}), today {today}"
        )

        daily = fetch_ga4_daily_metrics(client, daily_start, daily_end)

        with transaction.atomic():
            store_daily_metrics(daily)

        rows_written += len(daily["rows"])

        print(
            f"Daily metrics {daily_start} to {daily_end}: "
            f"{len(daily['rows'])} days"
        )

        for range_start, range_end, period_key in ranges:
            result = collect_ga4_range(
                range_start,
                range_end,
                period_key=period_key,
                client=client,
                include_daily=False,
            )

            rows_written += result["rows_written"]
            created_count += result["reports_created"]
            updated_count += result["reports_updated"]

            print(
                f"Range {range_start} to {range_end} "
                f"[{period_key}]: {result['rows_written']} rows"
            )

        log.status = "success"
        log.records_processed = rows_written
        log.error_message = ""
        log.completed_at = timezone.now()
        log.save(
            update_fields=[
                "status",
                "records_processed",
                "error_message",
                "completed_at",
            ]
        )

        return {
            "processed": rows_written,
            "created": created_count,
            "updated": updated_count,
            "failed": 0,
            "property": GA4_PROPERTY_ID,
            "property_timezone": property_timezone,
            "start_date": daily_start,
            "end_date": daily_end,
            "ranges": ranges,
        }

    except Exception as error:
        log.status = "failed"
        log.records_processed = rows_written
        log.error_message = str(error)
        log.completed_at = timezone.now()
        log.save(
            update_fields=[
                "status",
                "records_processed",
                "error_message",
                "completed_at",
            ]
        )

        raise
