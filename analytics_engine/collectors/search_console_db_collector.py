"""
Collect Search Console reports into the database.

Each logical report (see search_console_reports.py) is stored separately:

- SearchConsoleDailyMetric: one byProperty row per property + search type
  + date, labelled "final" or "preliminary".
- SearchConsoleReport + SearchConsoleReportRow: one report per property +
  search type + report type + exact date range, fetched with a single
  dataState ("final", or "all" while the range has unfinalized dates).

Re-collecting replaces values in place, so repeated runs are idempotent
and never accumulate duplicates. Every run re-fetches the recent window,
so preliminary values are replaced by Google's finalized values once
Google finalizes those dates.

Never sum query or page rows to produce site totals - read the "summary"
report for the same range instead.
"""

from decouple import config
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from analytics_engine.collectors.search_console_reports import (
    GSC_SEARCH_TYPE,
    GSC_SITE_URL,
    RANGE_REPORT_FETCHERS,
    create_search_console_service,
    dashboard_today,
    data_through,
    fetch_daily_range,
    fetch_freshness,
    gsc_today,
    preliminary_through,
    preset_range,
    report_data_state,
    validate_property_access,
)
from analytics_engine.models import (
    SearchConsoleDailyMetric,
    SearchConsoleReport,
    SearchConsoleReportRow,
    SystemLog,
)


SEARCH_CONSOLE_LOOKBACK_DAYS = config(
    "SEARCH_CONSOLE_LOOKBACK_DAYS",
    default=90,
    cast=int,
)

# Dashboard presets: the last N calendar dates ending today
# (DASHBOARD_TIME_ZONE).
PRESET_PERIODS = {
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
}

METRIC_FIELD_NAMES = ["clicks", "impressions", "ctr", "position"]


def report_identity():
    return {
        "site_url": GSC_SITE_URL,
        "search_type": GSC_SEARCH_TYPE,
    }


def metric_values(row):
    return {field: row[field] for field in METRIC_FIELD_NAMES}


def store_range_report(
    report_type,
    start_date,
    end_date,
    fetched,
    freshness,
    period_key="custom",
):
    """
    Upsert one range report and replace its rows.

    Must be called inside a transaction.
    """

    report, created = SearchConsoleReport.objects.get_or_create(
        **report_identity(),
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        defaults={"period_key": period_key},
    )

    # A custom request for the same dates as a preset must not hide the
    # preset from the dashboard.
    if period_key != "custom":
        report.period_key = period_key

    report.data_state = fetched["data_state"]
    report.aggregation_type = fetched["aggregation_type"]
    report.data_through = data_through(
        start_date,
        end_date,
        freshness["latest_final_date"],
    )
    report.preliminary_through = preliminary_through(
        start_date,
        end_date,
        freshness,
        fetched["data_state"],
    )
    report.row_count = len(fetched["rows"])
    report.save()

    report.rows.all().delete()

    SearchConsoleReportRow.objects.bulk_create(
        [
            SearchConsoleReportRow(
                report=report,
                dimension_value=row["dimension_value"],
                **metric_values(row),
            )
            for row in fetched["rows"]
        ]
    )

    return report, created


def store_daily_metrics(fetched):
    """
    Upsert daily rows in place (a preliminary row becomes final when
    Google finalizes the date) and remove stored rows for covered dates
    Google no longer returns, i.e. dates without impressions.

    Must be called inside a transaction. Returns rows written.
    """

    returned_dates = set()

    for row in fetched["rows"]:
        SearchConsoleDailyMetric.objects.update_or_create(
            **report_identity(),
            metric_date=row["metric_date"],
            defaults={
                **metric_values(row),
                "data_state": row["data_state"],
            },
        )
        returned_dates.add(row["metric_date"])

    covered = Q(pk__in=[])

    for date_range in (
        fetched["final_range"],
        fetched["preliminary_range"],
    ):
        if date_range:
            covered |= Q(metric_date__range=date_range)

    SearchConsoleDailyMetric.objects.filter(
        covered,
        **report_identity(),
    ).exclude(metric_date__in=returned_dates).delete()

    return len(fetched["rows"])


def fetch_range(service, start_date, end_date, freshness,
                include_daily=True):
    """Every range report (and optionally daily rows) for one range."""

    data_state = report_data_state(end_date, freshness)

    return {
        "reports": {
            report_type: fetcher(
                service,
                start_date,
                end_date,
                data_state=data_state,
            )
            for report_type, fetcher in RANGE_REPORT_FETCHERS.items()
        },
        "daily": (
            fetch_daily_range(service, start_date, end_date, freshness)
            if include_daily
            else None
        ),
    }


def store_range(start_date, end_date, fetched, period_key, freshness):
    """Store one fetched range. Must be called inside a transaction."""

    rows_written = 0
    reports_created = 0

    # Daily rows first: a report stored after them is current for them
    # (see is_range_current).
    if fetched["daily"]:
        rows_written += store_daily_metrics(fetched["daily"])

    for report_type, report in fetched["reports"].items():
        _, created = store_range_report(
            report_type,
            start_date,
            end_date,
            report,
            freshness,
            period_key=period_key,
        )

        rows_written += len(report["rows"])
        reports_created += int(created)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "period_key": period_key,
        "rows_written": rows_written,
        "reports_created": reports_created,
        "reports_updated": len(fetched["reports"]) - reports_created,
    }


def collect_search_console_range(
    start_date,
    end_date,
    period_key="custom",
    service=None,
    freshness=None,
):
    """
    Fetch and store every report, plus daily rows, for one exact range.

    All API requests run before the database is touched, so a failed
    request never leaves a partially refreshed range.
    """

    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")

    service = service or create_search_console_service()

    if freshness is None:
        freshness = fetch_freshness(service, dashboard_today())

    fetched = fetch_range(service, start_date, end_date, freshness)

    with transaction.atomic():
        return store_range(
            start_date,
            end_date,
            fetched,
            period_key,
            freshness,
        )


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

    # Every stored range that still includes unfinalized dates (custom
    # ranges and earlier days' presets alike) is refreshed so revised
    # preliminary values reach its KPIs, until Google has finalized all
    # of its dates. data_through only lags end_date for ranges ending in
    # the last few days, so this stays small.
    not_final = (
        SearchConsoleReport.objects.filter(
            **report_identity(),
            report_type=SearchConsoleReport.REPORT_SUMMARY,
        )
        .filter(
            Q(data_through__isnull=True)
            | Q(data_through__lt=F("end_date"))
        )
        .values_list("start_date", "end_date")
        .distinct()
    )

    for start_date, end_date in not_final:
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


def finish_log(log, status, rows_written, error_message=""):
    log.status = status
    log.records_processed = rows_written
    log.error_message = error_message
    log.completed_at = timezone.now()
    log.save(
        update_fields=[
            "status",
            "records_processed",
            "error_message",
            "completed_at",
        ]
    )


def collect_search_console_to_database(start_date=None, end_date=None):
    """
    Refresh Search Console data.

    Without arguments: daily rows for the lookback window (finalized
    dates with dataState "final", later dates with "all"), plus range
    reports for the dashboard presets, the previous completed week and
    custom ranges that are not fully finalized yet.

    With start_date/end_date: that exact range only (plus its daily rows).

    Every API request completes before anything is written, and all
    writes happen in one transaction, so a failed run changes nothing.
    """

    log = SystemLog.objects.create(
        module="search_console_collector",
        status="running",
        records_processed=0,
    )

    try:
        service = create_search_console_service()

        permission_level = validate_property_access(service)

        today = dashboard_today()
        freshness = fetch_freshness(service, today)

        if start_date and end_date:
            ranges = [(start_date, end_date, "custom")]
            daily_start, daily_end = start_date, end_date
        else:
            ranges = get_ranges_to_collect(today)
            daily_start, daily_end = preset_range(
                SEARCH_CONSOLE_LOOKBACK_DAYS,
                today,
            )

        print(
            f"Search Console property {GSC_SITE_URL} "
            f"({permission_level}), type {GSC_SEARCH_TYPE}"
        )
        print(
            f"Today {today} (Search Console date {gsc_today()}), "
            f"finalized through "
            f"{freshness['latest_final_date']}, preliminary data through "
            f"{freshness['latest_available_date']}"
        )

        daily = fetch_daily_range(service, daily_start, daily_end, freshness)

        fetched_ranges = [
            (
                range_start,
                range_end,
                period_key,
                fetch_range(
                    service,
                    range_start,
                    range_end,
                    freshness,
                    include_daily=False,
                ),
            )
            for range_start, range_end, period_key in ranges
        ]

        rows_written = 0
        created_count = 0
        updated_count = 0

        with transaction.atomic():
            rows_written += store_daily_metrics(daily)

            for range_start, range_end, period_key, fetched in (
                fetched_ranges
            ):
                result = store_range(
                    range_start,
                    range_end,
                    fetched,
                    period_key,
                    freshness,
                )

                rows_written += result["rows_written"]
                created_count += result["reports_created"]
                updated_count += result["reports_updated"]

                print(
                    f"Range {range_start} to {range_end} "
                    f"[{period_key}, dataState "
                    f"{fetched['reports']['summary']['data_state']}]: "
                    f"{result['rows_written']} rows"
                )

        states = [row["data_state"] for row in daily["rows"]]

        print(
            f"Daily rows {daily_start} to {daily_end}: "
            f"{states.count('final')} final, "
            f"{states.count('preliminary')} preliminary"
        )

        finish_log(log, "success", rows_written)

        return {
            "processed": rows_written,
            "created": created_count,
            "updated": updated_count,
            "failed": 0,
            "property": GSC_SITE_URL,
            "search_type": GSC_SEARCH_TYPE,
            "latest_final_date": freshness["latest_final_date"],
            "latest_available_date": freshness["latest_available_date"],
            "start_date": daily_start,
            "end_date": daily_end,
            "ranges": ranges,
        }

    except Exception as error:
        finish_log(log, "failed", 0, str(error))
        raise
