
from datetime import datetime, timedelta
from urllib.parse import urlparse
from google.oauth2.service_account import Credentials
from decouple import config
from decouple import config
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Metric,
    Dimension,
)




from analytics_engine.sheets.sheets_client import (
    read_content_library,
    append_ga4_rows,
    append_log,
)

from analytics_engine.processors.normalize import normalize_url


SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly"
]

credentials = Credentials.from_service_account_file(
    config("GOOGLE_CREDENTIALS_FILE"),
    scopes=SCOPES
)

PROPERTY_ID = config("GA4_PROPERTY_ID")


def get_ga4_trackable_urls():
    records = read_content_library()

    pages = []

    for row in records:
        website_url = normalize_url(row.get("website_url"))

        if not website_url:
            continue

        pages.append({
            "content_id": row.get("content_id"),
            "url": website_url,
        })

    return pages


def fetch_ga4_data():
    client = BetaAnalyticsDataClient(credentials=credentials)

    trackable_urls = get_ga4_trackable_urls()

    print("Trackable URLs found:", len(trackable_urls))
    print(trackable_urls[:5])

    rows_to_insert = []

    end_date = datetime.today().date()
    start_date = end_date - timedelta(days=90)
    date_range_text = f"{start_date} to {end_date}"

    try:
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[
                Dimension(name="pagePath"),
                Dimension(name="sessionSource"),
            ],
            metrics=[
                Metric(name="screenPageViews"),
                Metric(name="sessions"),
                Metric(name="totalUsers"),
                Metric(name="userEngagementDuration"),
            ],
            date_ranges=[
                DateRange(
                    start_date=str(start_date),
                    end_date=str(end_date),
                )
            ],
        )

        response = client.run_report(request)

        print("GA4 response rows:", len(response.rows))

        ga4_rows = []

        for row in response.rows:
            ga4_path = row.dimension_values[0].value
            traffic_source = row.dimension_values[1].value

            normalized_ga4_path = ga4_path.rstrip("/").lower() or "/"

            ga4_rows.append({
                "path": normalized_ga4_path,
                "traffic_source": traffic_source,
                "views": row.metric_values[0].value,
                "sessions": row.metric_values[1].value,
                "users": row.metric_values[2].value,
                "engagement": row.metric_values[3].value,
            })

        for item in trackable_urls:
            target_path = urlparse(item["url"]).path or "/"
            normalized_target_path = target_path.rstrip("/").lower() or "/"

            for ga4_item in ga4_rows:
                if ga4_item["path"] != normalized_target_path:
                    continue

                rows_to_insert.append([
                    item["content_id"],
                    item["url"],
                    date_range_text,
                    ga4_item["views"],
                    ga4_item["sessions"],
                    ga4_item["users"],
                    ga4_item["engagement"],
                    ga4_item["traffic_source"],
                    datetime.now().isoformat(),
                ])

                print("MATCHED:", {
                    "content_id": item["content_id"],
                    "target": normalized_target_path,
                    "ga4_path": ga4_item["path"],
                    "views": ga4_item["views"],
                })

        append_ga4_rows(rows_to_insert)

        append_log(
            "ga4_collector",
            "success",
            len(rows_to_insert),
            "",
        )

        print(f"GA4 rows inserted: {len(rows_to_insert)}")

    except Exception as e:
        append_log(
            "ga4_collector",
            "failed",
            0,
            str(e),
        )
        print("GA4 collector failed:", e)