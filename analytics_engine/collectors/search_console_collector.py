from datetime import datetime, timedelta
from urllib.parse import urlparse

from decouple import config

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from analytics_engine.sheets.sheets_client import (
    read_content_library,
    append_search_console_rows,
    append_log,
)

from analytics_engine.processors.normalize import normalize_url


SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly"
]

credentials = Credentials.from_service_account_file(
    config("GOOGLE_CREDENTIALS_FILE"),
    scopes=SCOPES
)

SITE_URL = config("SEARCH_CONSOLE_SITE_URL")


def get_trackable_urls():
    records = read_content_library()

    urls = []

    for row in records:

        website_url = normalize_url(row.get("website_url"))

        if not website_url:
            continue

        urls.append({
            "content_id": row.get("content_id"),
            "url": website_url,
        })

    return urls


def fetch_search_console_data():

    service = build(
        "searchconsole",
        "v1",
        credentials=credentials,
    )

    trackable_urls = get_trackable_urls()

    print("Trackable URLs:", len(trackable_urls))

    rows_to_insert = []

    end_date = datetime.today().date()
    start_date = end_date - timedelta(days=90)

    for item in trackable_urls:

        try:

            target_path = urlparse(item["url"]).path or "/"

            request = {
                "startDate": str(start_date),
                "endDate": str(end_date),
                "dimensions": ["page", "query"],
                "rowLimit": 250,
            }

            response = service.searchanalytics().query(
                siteUrl=SITE_URL,
                body=request,
            ).execute()

            rows = response.get("rows", [])

            print(f"Rows from Search Console: {len(rows)}")

            matched = False

            for row in rows:

                keys = row.get("keys", [])

                if not keys:
                    continue

                page_url = normalize_url(keys[0])

                page_path = urlparse(page_url).path.rstrip("/").lower()
                normalized_target_path = target_path.rstrip("/").lower()

                if page_path != normalized_target_path:
                    continue

                matched = True

                query = keys[1] if len(keys) > 1 else ""

                clicks = row.get("clicks", 0)
                impressions = row.get("impressions", 0)
                ctr = row.get("ctr", 0)
                position = row.get("position", 0)

                rows_to_insert.append([
                    item["content_id"],
                    item["url"],
                    f"{start_date} to {end_date}",
                    clicks,
                    impressions,
                    ctr,
                    position,
                    query,
                    datetime.now().isoformat(),
                ])

                print({
                    "content_id": item["content_id"],
                    "query": query,
                    "clicks": clicks,
                })

            if not matched:
                print(f"No Search Console match for: {item['url']}")

        except Exception as e:

            append_log(
                "search_console_collector",
                "failed",
                0,
                str(e),
            )

            print("Search Console error:", e)

    append_search_console_rows(rows_to_insert)

    append_log(
        "search_console_collector",
        "success",
        len(rows_to_insert),
        "",
    )

    print(f"Search Console rows inserted: {len(rows_to_insert)}")