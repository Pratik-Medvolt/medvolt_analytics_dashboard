"""
GA4 Data API report definitions.

Each logical dashboard dataset is its own GA4 query. This matters because
sessions and users are NON-ADDITIVE: a session that views three pages is
counted once per page row, a user active on two days once per date row.
Summing such rows over-counts (this is what previously turned GA4's 191
sessions / 167 users into 258 / 242 on the dashboard).

    fetch_ga4_summary()           no dimensions   -> KPI cards
    fetch_ga4_daily_metrics()     date            -> daily chart only
    fetch_ga4_page_performance()  pagePath        -> page table/chart only
    fetch_ga4_traffic_sources()   sessionSourceMedium
    fetch_ga4_channel_groups()    sessionDefaultChannelGroup

Everything here returns plain dicts and never touches the database, so the
verify_ga4 command can compare raw GA4 values against stored ones.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from decouple import config
from django.conf import settings
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.oauth2.service_account import Credentials


GA4_PROPERTY_ID = config(
    "GA4_PROPERTY_ID",
    default="",
).strip()

GOOGLE_CREDENTIALS_FILE = config(
    "GOOGLE_CREDENTIALS_FILE",
    default="google_credentials.json",
).strip()

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
]

# GA4 API page size; responses are paginated with offset.
GA4_PAGE_SIZE = 100000

# GA4 API metric name -> GA4MetricFields field name.
METRIC_FIELDS = {
    "screenPageViews": "views",
    "sessions": "sessions",
    "totalUsers": "total_users",
    "activeUsers": "active_users",
    "newUsers": "new_users",
    "engagedSessions": "engaged_sessions",
    "eventCount": "event_count",
    "userEngagementDuration": "engagement_duration_seconds",
}

SITE_METRICS = [
    "screenPageViews",
    "sessions",
    "totalUsers",
    "activeUsers",
    "newUsers",
    "engagedSessions",
    "eventCount",
    "userEngagementDuration",
]

# Matches GA4 "Pages and screens". Sessions are deliberately not requested:
# page-scoped session counts cannot be reconciled with site sessions.
PAGE_METRICS = [
    "screenPageViews",
    "activeUsers",
    "eventCount",
    "userEngagementDuration",
]

# Matches GA4 "Traffic acquisition".
ACQUISITION_METRICS = [
    "sessions",
    "engagedSessions",
    "totalUsers",
    "activeUsers",
    "eventCount",
    "userEngagementDuration",
]


def resolve_credentials_path():
    credentials_path = Path(GOOGLE_CREDENTIALS_FILE)

    if not credentials_path.is_absolute():
        credentials_path = Path(settings.BASE_DIR) / credentials_path

    if not credentials_path.exists():
        raise FileNotFoundError(
            "Google credentials file not found: "
            f"{credentials_path}"
        )

    return credentials_path


def create_ga4_client():
    if not GA4_PROPERTY_ID:
        raise ValueError(
            "GA4_PROPERTY_ID is missing from .env"
        )

    credentials = Credentials.from_service_account_file(
        str(resolve_credentials_path()),
        scopes=SCOPES,
    )

    return BetaAnalyticsDataClient(
        credentials=credentials
    )


def to_ga4_date(value):
    """
    GA4 date ranges are inclusive and interpreted in the property's
    timezone. Always send plain YYYY-MM-DD dates, never datetimes.
    """

    if isinstance(value, datetime):
        raise TypeError(
            "Pass a date, not a datetime, to avoid "
            "timezone conversion shifting the day."
        )

    if isinstance(value, date):
        return value.isoformat()

    # GA4 relative dates, resolved by GA4 in the property timezone.
    if value in ("today", "yesterday"):
        return value

    return date.fromisoformat(str(value)).isoformat()


def parse_ga4_date(value):
    return datetime.strptime(value, "%Y%m%d").date()


def parse_metric_value(metric_name, value):
    if metric_name == "userEngagementDuration":
        return float(value or 0)

    return int(float(value or 0))


def run_ga4_report(
    client,
    start_date,
    end_date,
    metrics,
    dimensions=(),
    order_by_metric=None,
):
    """
    Run one GA4 report (all pages) and return
    (rows, property_timezone). Each row is
    {"dimensions": [...], "metrics": {field_name: value}}.
    """

    rows = []
    property_timezone = ""
    offset = 0

    order_bys = []

    if order_by_metric:
        order_bys.append(
            OrderBy(
                metric=OrderBy.MetricOrderBy(
                    metric_name=order_by_metric
                ),
                desc=True,
            )
        )

    while True:
        request = RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            dimensions=[
                Dimension(name=name)
                for name in dimensions
            ],
            metrics=[
                Metric(name=name)
                for name in metrics
            ],
            date_ranges=[
                DateRange(
                    start_date=to_ga4_date(start_date),
                    end_date=to_ga4_date(end_date),
                )
            ],
            order_bys=order_bys,
            limit=GA4_PAGE_SIZE,
            offset=offset,
        )

        response = client.run_report(request)

        property_timezone = (
            response.metadata.time_zone
            or property_timezone
        )

        for row in response.rows:
            rows.append(
                {
                    "dimensions": [
                        value.value
                        for value in row.dimension_values
                    ],
                    "metrics": {
                        METRIC_FIELDS[name]: parse_metric_value(
                            name,
                            metric_value.value,
                        )
                        for name, metric_value in zip(
                            metrics,
                            row.metric_values,
                        )
                    },
                }
            )

        offset += len(response.rows)

        if not response.rows or offset >= response.row_count:
            break

    return rows, property_timezone


def empty_metrics(metrics):
    return {
        METRIC_FIELDS[name]: parse_metric_value(name, 0)
        for name in metrics
    }


def fetch_ga4_summary(client, start_date, end_date):
    """
    Site-wide totals for the range. The only valid source for the
    Sessions / Total Users / Active Users / New Users KPIs.
    """

    rows, property_timezone = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=SITE_METRICS,
    )

    metrics = (
        rows[0]["metrics"]
        if rows
        else empty_metrics(SITE_METRICS)
    )

    return {
        "property_timezone": property_timezone,
        "rows": [
            {"dimension_value": "", **metrics}
        ],
    }


def fetch_ga4_daily_metrics(client, start_date, end_date):
    """
    Date-dimension rows for the daily trend chart.

    Do not sum sessions/users across these rows to get a period total:
    a user active on several days is counted on each of them.
    """

    rows, property_timezone = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=SITE_METRICS,
        dimensions=["date"],
    )

    daily_rows = [
        {
            "metric_date": parse_ga4_date(row["dimensions"][0]),
            **row["metrics"],
        }
        for row in rows
    ]

    daily_rows.sort(key=lambda row: row["metric_date"])

    return {
        "property_timezone": property_timezone,
        "rows": daily_rows,
    }


def fetch_ga4_page_performance(client, start_date, end_date):
    """
    Page metrics by GA4 pagePath, stored exactly as GA4 reports them.

    Titles come from a second query because adding pageTitle as a
    dimension splits a path into one row per title, which would force
    summing non-additive active users per path.
    """

    rows, property_timezone = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=PAGE_METRICS,
        dimensions=["pagePath"],
        order_by_metric="screenPageViews",
    )

    title_rows, _ = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=["screenPageViews"],
        dimensions=["pagePath", "pageTitle"],
        order_by_metric="screenPageViews",
    )

    # Rows are ordered by views, so the first title seen per path is the
    # one with the most views.
    titles = {}

    for row in title_rows:
        page_path, page_title = row["dimensions"]

        if page_title and page_title != "(not set)":
            titles.setdefault(page_path, page_title.strip())

    return {
        "property_timezone": property_timezone,
        "rows": [
            {
                "dimension_value": row["dimensions"][0],
                "page_title": titles.get(row["dimensions"][0], ""),
                **row["metrics"],
            }
            for row in rows
        ],
    }


def fetch_ga4_traffic_sources(client, start_date, end_date):
    """Sessions by session source / medium (GA4 Traffic acquisition)."""

    rows, property_timezone = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=ACQUISITION_METRICS,
        dimensions=["sessionSourceMedium"],
        order_by_metric="sessions",
    )

    return {
        "property_timezone": property_timezone,
        "rows": [
            {
                "dimension_value": row["dimensions"][0],
                **row["metrics"],
            }
            for row in rows
        ],
    }


def fetch_ga4_channel_groups(client, start_date, end_date):
    """Sessions by session default channel group."""

    rows, property_timezone = run_ga4_report(
        client,
        start_date,
        end_date,
        metrics=ACQUISITION_METRICS,
        dimensions=["sessionDefaultChannelGroup"],
        order_by_metric="sessions",
    )

    return {
        "property_timezone": property_timezone,
        "rows": [
            {
                "dimension_value": row["dimensions"][0],
                **row["metrics"],
            }
            for row in rows
        ],
    }


# report_type -> fetch function for range-scoped reports.
RANGE_REPORT_FETCHERS = {
    "summary": fetch_ga4_summary,
    "source_medium": fetch_ga4_traffic_sources,
    "channel_group": fetch_ga4_channel_groups,
    "page": fetch_ga4_page_performance,
}


def get_property_today(client):
    """
    "Today" in the GA4 property's timezone.

    The server runs in UTC but GA4 dates are property-local, so using the
    server date for "yesterday" is off by one for part of each day.
    """

    _, property_timezone = run_ga4_report(
        client,
        "today",
        "today",
        metrics=["sessions"],
    )

    return property_today(property_timezone), property_timezone


def property_today(property_timezone):
    try:
        zone = ZoneInfo(property_timezone)
    except Exception:
        zone = ZoneInfo(settings.TIME_ZONE)

    return datetime.now(zone).date()


def preset_range(days, today):
    """
    GA4 UI "Last N days": N full days ending yesterday, inclusive.
    """

    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)

    return start_date, end_date
