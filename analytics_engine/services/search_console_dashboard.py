"""
Search Console dashboard data.

Every value comes from the Search Console report that defines it for the
exact selected range:

    summary  <- SearchConsoleReport "summary" (no dimensions, byProperty)
    daily    <- SearchConsoleDailyMetric      (date, byProperty; chart only)
    queries  <- SearchConsoleReport "query"   (byProperty)
    pages    <- SearchConsoleReport "page"    (byPage)

Do not "fix" a missing summary by summing query or page rows: Google
omits anonymized queries and counts page impressions per page.

Dates are Search Console (Pacific Time) dates. Each selected date is:

    final        finalized by Google
    preliminary  included from dataState "all"; Google may still revise it
    pending      Google has no data for it yet - never shown as zero

All four datasets of a range come from reports fetched with the same
dataState, and the chart shows exactly the dates the KPIs include.
"""

from datetime import timedelta
from urllib.parse import unquote, urlsplit

from django.db.models import Max

from analytics_engine.collectors.ga4_db_collector import WEBSITE_BASE_URL
from analytics_engine.collectors.ga4_reports import GA4_PROPERTY_ID
from analytics_engine.collectors.search_console_db_collector import (
    PRESET_PERIODS,
    report_identity,
)
from analytics_engine.collectors.search_console_reports import (
    DASHBOARD_TIME_ZONE,
    GSC_SEARCH_TYPE,
    GSC_SITE_URL,
    GSC_TIMEZONE,
)
from analytics_engine.models import (
    Content,
    GA4Report,
    GA4ReportRow,
    SearchConsoleDailyMetric,
    SearchConsoleReport,
    SystemLog,
)


# Content titles from these sources are derived from the page itself.
# Legacy "ga4_api" titles are not trusted: they were taken from GA4 page
# views whose title belonged to the previous page of a client-side
# navigation (e.g. "Pipeline" stored for /about-us).
TRUSTED_CONTENT_TITLE_SOURCES = {"content_discovery"}


def ctr_percent(clicks, impressions):
    """CTR = clicks / impressions * 100, unrounded; None without impressions."""

    if not impressions:
        return None

    return clicks / impressions * 100


def get_preset_range(days):
    """Latest collected (start_date, end_date) for an "N Days" preset."""

    period_key = f"last_{days}_days"

    if period_key not in PRESET_PERIODS:
        return None

    report = (
        SearchConsoleReport.objects.filter(
            **report_identity(),
            report_type=SearchConsoleReport.REPORT_SUMMARY,
            period_key=period_key,
        )
        .order_by("-end_date")
        .first()
    )

    if not report:
        return None

    return report.start_date, report.end_date


def get_range_reports(start_date, end_date):
    reports = SearchConsoleReport.objects.filter(
        **report_identity(),
        start_date=start_date,
        end_date=end_date,
    )

    return {report.report_type: report for report in reports}


def get_latest_available_date():
    """Latest date with any (final or preliminary) data stored."""

    return SearchConsoleDailyMetric.objects.filter(
        **report_identity()
    ).aggregate(latest=Max("metric_date"))["latest"]


def get_latest_final_date():
    """Latest finalized date stored for the property."""

    return SearchConsoleDailyMetric.objects.filter(
        **report_identity(),
        data_state=SearchConsoleDailyMetric.STATE_FINAL,
    ).aggregate(latest=Max("metric_date"))["latest"]


def clamp(value, start_date, end_date):
    """value limited to the range, or None if before it."""

    if value is None or value < start_date:
        return None

    return min(value, end_date)


def is_range_current(start_date, end_date):
    """
    True if every report for the range is stored and the collector has
    not since stored newer final or preliminary dates for the range, or
    re-fetched (possibly revised) preliminary values for it.
    """

    reports = get_range_reports(start_date, end_date)

    if not set(reports) >= {
        report_type
        for report_type, _ in SearchConsoleReport.REPORT_TYPE_CHOICES
    }:
        return False

    summary = reports[SearchConsoleReport.REPORT_SUMMARY]
    before_range = start_date - timedelta(days=1)

    expected_final = clamp(get_latest_final_date(), start_date, end_date)
    expected_any = clamp(get_latest_available_date(), start_date, end_date)

    if not (
        (summary.data_through or before_range)
        >= (expected_final or before_range)
        and (summary.covered_through or before_range)
        >= (expected_any or before_range)
    ):
        return False

    # Preliminary values the collector has re-fetched since this report
    # was stored may have been revised by Google.
    refreshed_after = SearchConsoleDailyMetric.objects.filter(
        **report_identity(),
        data_state=SearchConsoleDailyMetric.STATE_PRELIMINARY,
        metric_date__range=(start_date, end_date),
        collected_at__gt=summary.collected_at,
    )

    return not refreshed_after.exists()


# ---------------------------------------------------------------------------
# Page titles
# ---------------------------------------------------------------------------


def page_lookup_key(url):
    """
    Key used only to look up a title for a page URL: host without "www.",
    percent-decoded path without a trailing slash (GA4 reports "/a b"
    where Search Console reports "/a%20b"). Scheme, query string and
    fragment are ignored. Path case is preserved. The stored URL is
    never changed.
    """

    parts = urlsplit(str(url or "").strip())

    host = (parts.hostname or "").lower()

    if host.startswith("www."):
        host = host[4:]

    return host, normalize_path(parts.path)


def normalize_path(path):
    path = unquote(path or "/")

    return path.rstrip("/") or "/"


def website_host():
    return page_lookup_key(WEBSITE_BASE_URL)[0]


def ga4_page_titles():
    """
    GA4 page path -> most-viewed page title, from the widest collected
    GA4 page report. GA4 paths have no host, so they only apply to URLs
    on the main website host.
    """

    reports = GA4Report.objects.filter(
        property_id=GA4_PROPERTY_ID,
        report_type=GA4Report.REPORT_PAGE,
    )

    report = (
        reports.filter(period_key="last_90_days")
        .order_by("-end_date")
        .first()
        or reports.order_by("-end_date", "start_date").first()
    )

    if not report:
        return {}

    titles = {}

    for path, title in (
        GA4ReportRow.objects.filter(report=report)
        .exclude(page_title="")
        .values_list("dimension_value", "page_title")
    ):
        titles.setdefault(normalize_path(path), title)

    return titles


def content_page_titles():
    """Lookup key -> title for website content with a trusted title."""

    titles = {}

    for content in (
        Content.objects.filter(
            platform="website",
            source__in=TRUSTED_CONTENT_TITLE_SOURCES,
        )
        .exclude(title="")
        .order_by("content_id")
    ):
        for url in (content.canonical_url, content.website_url):
            if url:
                titles.setdefault(page_lookup_key(url), content.title)

    return titles


def resolve_page_titles(urls):
    """
    Map each page URL, exactly as Google returned it, to a title.

    Titles match only the same host (www-insensitive) and the same path;
    a URL with no reliable title gets None so the page shows its URL.
    """

    ga4_titles = ga4_page_titles()
    content_titles = content_page_titles()
    site_host = website_host()

    resolved = {}

    for url in urls:
        key = page_lookup_key(url)

        title = content_titles.get(key)

        if key[0] == site_host:
            title = ga4_titles.get(key[1]) or title

        resolved[url] = title

    return resolved


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def serialize_metrics(row):
    return {
        "clicks": row.clicks,
        "impressions": row.impressions,
        # Google's ratio (0-1), unrounded.
        "ctr": row.ctr,
        "ctr_percent": ctr_percent(row.clicks, row.impressions),
        "position": row.position,
    }


def serialize_summary(report):
    if not report:
        return None

    row = report.rows.first()

    return serialize_metrics(row) if row else None


def serialize_queries(report):
    if not report:
        return []

    return [
        {"query": row.dimension_value, **serialize_metrics(row)}
        for row in report.rows.order_by(
            "-clicks",
            "-impressions",
            "dimension_value",
        )
    ]


def serialize_pages(report):
    if not report:
        return []

    rows = list(
        report.rows.order_by("-clicks", "-impressions", "dimension_value")
    )

    titles = resolve_page_titles(row.dimension_value for row in rows)

    return [
        {
            "page_url": row.dimension_value,
            "page_path": page_lookup_key(row.dimension_value)[1],
            "page_title": titles.get(row.dimension_value),
            **serialize_metrics(row),
        }
        for row in rows
    ]


def empty_day(day, status):
    zero = status != "pending"

    return {
        "date": day,
        "status": status,
        "clicks": 0 if zero else None,
        "impressions": 0 if zero else None,
        "ctr": None,
        "ctr_percent": None,
        "position": None,
    }


def serialize_daily(start_date, end_date, final_through, covered_through):
    """
    One entry per selected date, limited to the dates the range's KPIs
    include. Covered dates missing from Google's date report had no
    impressions and are real zeros; dates after the covered range are
    "pending" with null metrics.
    """

    rows = {
        row.metric_date: row
        for row in SearchConsoleDailyMetric.objects.filter(
            **report_identity(),
            metric_date__range=(start_date, end_date),
        )
    }

    daily = []
    day = start_date

    while day <= end_date:
        row = rows.get(day)

        if covered_through is None or day > covered_through:
            daily.append(empty_day(day, "pending"))
        elif row:
            daily.append(
                {
                    "date": day,
                    "status": row.data_state,
                    **serialize_metrics(row),
                }
            )
        else:
            final = final_through is not None and day <= final_through
            daily.append(
                empty_day(day, "final" if final else "preliminary")
            )

        day += timedelta(days=1)

    return daily


def serialize_collector():
    logs = SystemLog.objects.filter(module="search_console_collector")

    log = logs.order_by("-started_at").first()

    if not log:
        return None

    last_success = (
        logs.filter(status="success").order_by("-started_at").first()
    )

    return {
        "status": log.status,
        "started_at": log.started_at,
        "completed_at": log.completed_at,
        # Database rows written by the run - not a traffic metric.
        "rows_written": log.records_processed,
        "error_message": log.error_message,
        "last_success_at": (
            last_success.completed_at if last_success else None
        ),
    }


def build_search_console_payload(start_date, end_date):
    reports = get_range_reports(start_date, end_date)

    summary_report = reports.get(SearchConsoleReport.REPORT_SUMMARY)

    missing_reports = [
        report_type
        for report_type, _ in SearchConsoleReport.REPORT_TYPE_CHOICES
        if report_type not in reports
    ]

    final_through = (
        summary_report.data_through if summary_report else None
    )
    covered_through = (
        summary_report.covered_through if summary_report else None
    )
    preliminary_end = (
        summary_report.preliminary_through if summary_report else None
    )
    preliminary_start = (
        (
            final_through + timedelta(days=1)
            if final_through
            else start_date
        )
        if preliminary_end
        else None
    )

    pending_start = (
        covered_through + timedelta(days=1)
        if covered_through
        else start_date
    )

    has_pending = summary_report is not None and pending_start <= end_date

    synced_times = [report.collected_at for report in reports.values()]

    return {
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            "days": (end_date - start_date).days + 1,
            "site_url": GSC_SITE_URL,
            "search_type": GSC_SEARCH_TYPE,
            # dataState used for every report of this range.
            "data_state": (
                summary_report.data_state if summary_report else None
            ),
            # Google's date labels, and the zone that defines "today".
            "timezone": GSC_TIMEZONE,
            "dashboard_timezone": DASHBOARD_TIME_ZONE,
            # Latest finalized date the KPIs for this range include.
            "data_through": final_through,
            # Preliminary dates the KPIs include, if any.
            "preliminary_start": preliminary_start,
            "preliminary_end": preliminary_end,
            "preliminary_days": (
                (preliminary_end - preliminary_start).days + 1
                if preliminary_end
                else 0
            ),
            # Latest date (final or preliminary) the KPIs include.
            "available_through": covered_through,
            # Selected dates with final or preliminary data (including
            # real zero days); the rest are pending at Google.
            "available_days": (
                (covered_through - start_date).days + 1
                if covered_through
                else 0
            ),
            # Latest dates stored for the property overall.
            "latest_final_date": get_latest_final_date(),
            "latest_available_date": get_latest_available_date(),
            "pending_start": pending_start if has_pending else None,
            "pending_end": end_date if has_pending else None,
            "pending_days": (
                (end_date - pending_start).days + 1 if has_pending else 0
            ),
            # Oldest report sync, i.e. how fresh the whole view is.
            "last_synced": min(synced_times) if synced_times else None,
            "missing_reports": missing_reports,
        },
        "summary": serialize_summary(summary_report),
        "daily": (
            serialize_daily(
                start_date,
                end_date,
                final_through,
                covered_through,
            )
            if summary_report
            else []
        ),
        "queries": serialize_queries(
            reports.get(SearchConsoleReport.REPORT_QUERY)
        ),
        "pages": serialize_pages(
            reports.get(SearchConsoleReport.REPORT_PAGE)
        ),
        "collector": serialize_collector(),
    }


def get_range_summary(start_date, end_date):
    """Property totals row for an exact range, or None if not collected."""

    report = get_range_reports(start_date, end_date).get(
        SearchConsoleReport.REPORT_SUMMARY
    )

    return report.rows.first() if report else None


def get_daily_totals(start_date, end_date):
    """
    Property clicks and impressions summed over daily rows. Valid because
    byProperty date rows partition the property's clicks and impressions;
    CTR must be recomputed from these sums, never averaged.
    """

    rows = SearchConsoleDailyMetric.objects.filter(
        **report_identity(),
        metric_date__range=(start_date, end_date),
    )

    return {
        "clicks": sum(row.clicks for row in rows),
        "impressions": sum(row.impressions for row in rows),
    }
