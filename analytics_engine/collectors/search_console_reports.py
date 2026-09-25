"""
Search Console Search Analytics API report definitions.

Each logical dashboard dataset is its own Search Analytics query, because
Google aggregates each one differently:

    fetch_gsc_summary()  no dimensions, byProperty -> KPI cards
    fetch_gsc_daily()    date, byProperty          -> daily chart
    fetch_gsc_queries()  query, byProperty         -> query table only
    fetch_gsc_pages()    page, byPage              -> page table only

Query rows exclude anonymized queries and page rows count an impression
for every page shown in a result, so neither sums to the property total.
Summing date x page x query rows is what previously turned Google's
23 clicks / 740 impressions into 15 / 280 on the dashboard.

Every request uses the same property and search type (web). Google
finalizes a date 2-3 days later; before that the date is only returned
with dataState "all" and its values are preliminary:

    finalized dates     dataState "final"
    recent dates        dataState "all", labelled from firstIncompleteDate
    no data yet         not returned; shown as pending, never as zero

All reports for one range use a single dataState (report_data_state) so
KPIs, chart, queries and pages always describe the same data.

Everything here returns plain dicts and never touches the database, so
the verify_gsc command can compare raw API values with stored ones.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from decouple import config
from django.conf import settings
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


GSC_SITE_URL = config(
    "SEARCH_CONSOLE_SITE_URL",
    default="",
).strip()

GOOGLE_CREDENTIALS_FILE = config(
    "GOOGLE_CREDENTIALS_FILE",
    default="google_credentials.json",
).strip()

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
]

# The Search Console "Web" search type, as in the Performance report.
GSC_SEARCH_TYPE = "web"

# Finalized data. Preliminary data for the last ~2 days is requested
# separately with GSC_DATA_STATE_ALL and labelled preliminary.
GSC_DATA_STATE = "final"
GSC_DATA_STATE_ALL = "all"

# Daily row labels (SearchConsoleDailyMetric.data_state).
STATE_FINAL = "final"
STATE_PRELIMINARY = "preliminary"

# Search Console labels performance dates in Pacific Time, regardless of
# the server or GA4 property timezone.
GSC_TIMEZONE = "America/Los_Angeles"

# The dashboard's reporting timezone: which calendar day is "today" for
# the 7 / 30 / 90 Days presets. Independent of Django's TIME_ZONE (UTC,
# used for timestamps) and of GSC_TIMEZONE (Google's date labels).
DASHBOARD_TIME_ZONE = config(
    "DASHBOARD_TIME_ZONE",
    default="Asia/Kolkata",
).strip()

# The API returns at most 25,000 rows per request; larger reports are
# paginated with startRow.
GSC_MAX_ROW_LIMIT = 25000

GSC_ROW_LIMIT = min(
    config(
        "SEARCH_CONSOLE_ROW_LIMIT",
        default=GSC_MAX_ROW_LIMIT,
        cast=int,
    ),
    GSC_MAX_ROW_LIMIT,
)

# Search Console keeps 16 months of performance data.
GSC_RETENTION_DAYS = 486

# Days before today re-fetched with dataState "all" to find the latest
# finalized and preliminary dates.
FRESHNESS_WINDOW_DAYS = 10


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


def create_search_console_service():
    if not GSC_SITE_URL:
        raise ValueError(
            "SEARCH_CONSOLE_SITE_URL is missing from .env"
        )

    credentials = Credentials.from_service_account_file(
        str(resolve_credentials_path()),
        scopes=SCOPES,
    )

    return build(
        "searchconsole",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def validate_property_access(service):
    """
    Confirm the service account can read the configured property and
    return its permission level.

    The property must match exactly: "sc-domain:medvolt.ai" (all
    subdomains and protocols) and "https://www.medvolt.ai/" (one URL
    prefix) are different properties with different totals.
    """

    sites = service.sites().list().execute().get("siteEntry", [])

    for site in sites:
        if site.get("siteUrl") == GSC_SITE_URL:
            return site.get("permissionLevel", "unknown")

    raise PermissionError(
        "The configured Search Console property is not available to "
        f"the service account. Configured: {GSC_SITE_URL}. "
        f"Available: {[site.get('siteUrl') for site in sites]}"
    )


def to_gsc_date(value):
    """
    Search Analytics date ranges are inclusive Pacific Time dates. Always
    send plain YYYY-MM-DD dates, never datetimes.
    """

    if isinstance(value, datetime):
        raise TypeError(
            "Pass a date, not a datetime, to avoid "
            "timezone conversion shifting the day."
        )

    if isinstance(value, date):
        return value.isoformat()

    return date.fromisoformat(str(value)).isoformat()


def gsc_today():
    """Today's date as Search Console labels it (Pacific Time)."""

    return datetime.now(ZoneInfo(GSC_TIMEZONE)).date()


def dashboard_today():
    """
    The selected calendar day: today in DASHBOARD_TIME_ZONE (India by
    default), so at 00:30 IST the presets already end on the new day even
    though it is still the previous day in UTC and in Pacific Time.

    Presets end on this day even when Google (which labels dates in
    Pacific Time) has no data for it yet; such dates are shown as
    pending. The selected range is never shifted to Google's latest
    available date.
    """

    return datetime.now(ZoneInfo(DASHBOARD_TIME_ZONE)).date()


def preset_range(days, today):
    """
    Dashboard "N Days" preset: the last N calendar dates ending today,
    inclusive (on 25 Sep, 7 Days = 19-25 Sep). The most recent ~2-3
    dates are preliminary, and dates Google has no data for yet are
    pending.
    """

    return today - timedelta(days=days - 1), today


def parse_metrics(row):
    impressions = int(round(float(row.get("impressions") or 0)))

    return {
        "clicks": int(round(float(row.get("clicks") or 0))),
        "impressions": impressions,
        "ctr": float(row["ctr"]) if impressions else None,
        "position": float(row["position"]) if impressions else None,
    }


def empty_metrics():
    return {
        "clicks": 0,
        "impressions": 0,
        "ctr": None,
        "position": None,
    }


def run_search_analytics_query(
    service,
    start_date,
    end_date,
    dimensions=(),
    aggregation_type="byProperty",
    data_state=GSC_DATA_STATE,
):
    """
    Run one Search Analytics query, following startRow pagination until
    Google returns a short page.

    Pagination retrieves every row Google serves; it cannot recover
    anonymized queries or rows beyond Google's serving limits.

    Returns {"rows": [{"keys": [...], **metrics}],
             "aggregation_type": str, "metadata": dict}.
    """

    rows = []
    start_row = 0
    aggregation = ""
    metadata = {}

    while True:
        body = {
            "startDate": to_gsc_date(start_date),
            "endDate": to_gsc_date(end_date),
            "type": GSC_SEARCH_TYPE,
            "dataState": data_state,
            "aggregationType": aggregation_type,
            "rowLimit": GSC_ROW_LIMIT,
            "startRow": start_row,
        }

        if dimensions:
            body["dimensions"] = list(dimensions)

        response = (
            service.searchanalytics()
            .query(siteUrl=GSC_SITE_URL, body=body)
            .execute()
        )

        page = response.get("rows", [])

        aggregation = (
            response.get("responseAggregationType") or aggregation
        )
        metadata = response.get("metadata") or metadata

        rows.extend(
            {"keys": row.get("keys", []), **parse_metrics(row)}
            for row in page
        )

        if len(page) < GSC_ROW_LIMIT:
            break

        start_row += len(page)

    return {
        "rows": rows,
        "aggregation_type": aggregation,
        "metadata": metadata,
    }


def sort_by_traffic(rows):
    return sorted(
        rows,
        key=lambda row: (
            -row["clicks"],
            -row["impressions"],
            row["dimension_value"],
        ),
    )


def fetch_gsc_summary(service, start_date, end_date,
                      data_state=GSC_DATA_STATE):
    """
    Property totals for the range. The only valid source for the Clicks,
    Impressions, CTR and Average Position KPIs.
    """

    result = run_search_analytics_query(
        service,
        start_date,
        end_date,
        data_state=data_state,
    )

    metrics = (
        {
            key: value
            for key, value in result["rows"][0].items()
            if key != "keys"
        }
        if result["rows"]
        else empty_metrics()
    )

    return {
        "aggregation_type": result["aggregation_type"],
        "data_state": data_state,
        "rows": [{"dimension_value": "", **metrics}],
    }


def fetch_gsc_daily(service, start_date, end_date,
                    data_state=GSC_DATA_STATE):
    """
    Date-dimension property rows. Dates without any impressions are
    omitted by Google. With dataState "all", Google's metadata gives the
    first date that is not finalized yet.
    """

    result = run_search_analytics_query(
        service,
        start_date,
        end_date,
        dimensions=["date"],
        data_state=data_state,
    )

    # With dataState "all", Google returns a placeholder row (0 clicks,
    # 0 impressions, position 0) for dates it has no data for yet, such
    # as today. Real dates without impressions are omitted instead, so a
    # zero-impression row means "no data yet": leave it out so the date
    # is shown as pending rather than as zero traffic.
    rows = [
        {
            "metric_date": date.fromisoformat(row["keys"][0]),
            **{key: value for key, value in row.items() if key != "keys"},
        }
        for row in result["rows"]
        if row["impressions"] > 0
    ]

    rows.sort(key=lambda row: row["metric_date"])

    first_incomplete = result["metadata"].get("firstIncompleteDate")

    return {
        "aggregation_type": result["aggregation_type"],
        "data_state": data_state,
        "first_incomplete_date": (
            date.fromisoformat(first_incomplete)
            if first_incomplete
            else None
        ),
        "rows": rows,
    }


def fetch_dimension_report(
    service,
    start_date,
    end_date,
    dimension,
    aggregation_type,
    data_state,
):
    result = run_search_analytics_query(
        service,
        start_date,
        end_date,
        dimensions=[dimension],
        aggregation_type=aggregation_type,
        data_state=data_state,
    )

    return {
        "aggregation_type": result["aggregation_type"],
        "data_state": data_state,
        "rows": sort_by_traffic(
            [
                {
                    "dimension_value": row["keys"][0],
                    **{
                        key: value
                        for key, value in row.items()
                        if key != "keys"
                    },
                }
                for row in result["rows"]
            ]
        ),
    }


def fetch_gsc_queries(service, start_date, end_date,
                      data_state=GSC_DATA_STATE):
    """
    Query rows. Anonymized queries are omitted by Google, so these do not
    sum to the property totals.
    """

    return fetch_dimension_report(
        service, start_date, end_date, "query", "byProperty", data_state
    )


def fetch_gsc_pages(service, start_date, end_date,
                    data_state=GSC_DATA_STATE):
    """
    Page rows aggregated by page, with each URL exactly as Google returns
    it. Impressions are counted per page, so they do not sum to the
    property total.
    """

    return fetch_dimension_report(
        service, start_date, end_date, "page", "byPage", data_state
    )


# report_type -> fetch function for range-scoped reports.
RANGE_REPORT_FETCHERS = {
    "summary": fetch_gsc_summary,
    "query": fetch_gsc_queries,
    "page": fetch_gsc_pages,
}


def fetch_freshness(service, today):
    """
    What Google has for the recent window, from one dataState "all" date
    query:

        latest_final_date      day before Google's firstIncompleteDate
        latest_available_date  latest date with any (preliminary) data

    Either is None when no recent data exists.
    """

    daily = fetch_gsc_daily(
        service,
        today - timedelta(days=FRESHNESS_WINDOW_DAYS),
        today,
        data_state=GSC_DATA_STATE_ALL,
    )

    dates = [row["metric_date"] for row in daily["rows"]]
    latest_available = max(dates) if dates else None

    if daily["first_incomplete_date"]:
        latest_final = daily["first_incomplete_date"] - timedelta(days=1)
    else:
        # No incomplete dates reported: every returned date is final.
        latest_final = latest_available

    if latest_available is None or (
        latest_final is not None and latest_final > latest_available
    ):
        latest_available = latest_final

    return {
        "latest_final_date": latest_final,
        "latest_available_date": latest_available,
    }


def report_data_state(end_date, freshness):
    """
    The one dataState used for every report of a range, so KPIs, chart,
    queries and pages describe the same data: "final" when the whole
    range is finalized, otherwise "all".
    """

    latest_final = freshness["latest_final_date"]

    if latest_final is not None and end_date <= latest_final:
        return GSC_DATA_STATE

    return GSC_DATA_STATE_ALL


def fetch_daily_range(service, start_date, end_date, freshness):
    """
    Daily rows for the range, each labelled "final" or "preliminary".

    Finalized dates are fetched with dataState "final". Later dates are
    fetched with dataState "all" and are labelled preliminary from
    Google's firstIncompleteDate in that same response.

    Returns {"rows": [...], "final_range": (start, end) or None,
             "preliminary_range": (start, end) or None}; the ranges are
    the dates each request covered.
    """

    latest_final = freshness["latest_final_date"]
    latest_available = freshness["latest_available_date"]

    rows = []
    final_range = None
    preliminary_range = None

    if latest_final is not None and latest_final >= start_date:
        final_range = (start_date, min(end_date, latest_final))

        for row in fetch_gsc_daily(service, *final_range)["rows"]:
            rows.append({**row, "data_state": STATE_FINAL})

    preliminary_start = (
        max(start_date, latest_final + timedelta(days=1))
        if latest_final is not None
        else start_date
    )

    if (
        latest_available is not None
        and preliminary_start <= end_date
        and preliminary_start <= latest_available
    ):
        preliminary_range = (preliminary_start, end_date)

        daily = fetch_gsc_daily(
            service,
            *preliminary_range,
            data_state=GSC_DATA_STATE_ALL,
        )

        first_incomplete = daily["first_incomplete_date"]

        for row in daily["rows"]:
            finalized = (
                first_incomplete is not None
                and row["metric_date"] < first_incomplete
            )
            rows.append(
                {
                    **row,
                    "data_state": (
                        STATE_FINAL if finalized else STATE_PRELIMINARY
                    ),
                }
            )

    return {
        "rows": rows,
        "final_range": final_range,
        "preliminary_range": preliminary_range,
    }


def data_through(start_date, end_date, latest_final_date):
    """Latest finalized date inside the range, or None."""

    if latest_final_date is None or latest_final_date < start_date:
        return None

    return min(end_date, latest_final_date)


def preliminary_through(start_date, end_date, freshness, data_state):
    """
    Latest preliminary date included in an "all" report for the range,
    or None.
    """

    latest_final = freshness["latest_final_date"]
    latest_available = freshness["latest_available_date"]

    if data_state != GSC_DATA_STATE_ALL or latest_available is None:
        return None

    if latest_final is not None and latest_available <= latest_final:
        return None

    if latest_available < start_date:
        return None

    return min(end_date, latest_available)
