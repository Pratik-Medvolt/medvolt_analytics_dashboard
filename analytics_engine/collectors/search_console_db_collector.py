from datetime import datetime, timedelta
from pathlib import Path

from decouple import config
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from google.oauth2.service_account import (
    Credentials,
)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from analytics_engine.models import (
    SearchConsoleAnalytics,
    SystemLog,
)
from analytics_engine.services.content_registry import (
    get_or_create_website_content,
    normalize_content_url,
)


SEARCH_CONSOLE_SITE_URL = config(
    "SEARCH_CONSOLE_SITE_URL",
    default="",
).strip()

GOOGLE_CREDENTIALS_FILE = config(
    "GOOGLE_CREDENTIALS_FILE",
    default="google_credentials.json",
).strip()

SEARCH_CONSOLE_LOOKBACK_DAYS = config(
    "SEARCH_CONSOLE_LOOKBACK_DAYS",
    default=90,
    cast=int,
)

SEARCH_CONSOLE_END_LAG_DAYS = config(
    "SEARCH_CONSOLE_END_LAG_DAYS",
    default=3,
    cast=int,
)

SEARCH_CONSOLE_ROW_LIMIT = config(
    "SEARCH_CONSOLE_ROW_LIMIT",
    default=25000,
    cast=int,
)

SCOPES = [
    (
        "https://www.googleapis.com/auth/"
        "webmasters.readonly"
    )
]


def clean_int(value):
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def clean_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def resolve_credentials_path():
    credentials_path = Path(
        GOOGLE_CREDENTIALS_FILE
    )

    if not credentials_path.is_absolute():
        credentials_path = (
            Path(settings.BASE_DIR)
            / credentials_path
        )

    if not credentials_path.exists():
        raise FileNotFoundError(
            "Google credentials file not found: "
            f"{credentials_path}"
        )

    return credentials_path


def create_search_console_service():
    if not SEARCH_CONSOLE_SITE_URL:
        raise ValueError(
            "SEARCH_CONSOLE_SITE_URL is missing "
            "from the .env file."
        )

    credentials = (
        Credentials.from_service_account_file(
            str(resolve_credentials_path()),
            scopes=SCOPES,
        )
    )

    return build(
        "searchconsole",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def validate_property_access(service):
    """
    Confirm that the authenticated service account
    can access the configured Search Console property.
    """

    response = (
        service.sites()
        .list()
        .execute()
    )

    accessible_sites = response.get(
        "siteEntry",
        [],
    )

    for site in accessible_sites:
        if (
            site.get("siteUrl")
            == SEARCH_CONSOLE_SITE_URL
        ):
            return site.get(
                "permissionLevel",
                "unknown",
            )

    available_properties = [
        site.get("siteUrl")
        for site in accessible_sites
    ]

    raise PermissionError(
        "The configured Search Console property "
        "is not available to the service account. "
        f"Configured: {SEARCH_CONSOLE_SITE_URL}. "
        f"Available: {available_properties}"
    )


def fetch_search_console_rows(
    service,
    start_date,
    end_date,
):
    """
    Retrieve all available Search Console rows using
    pagination instead of making one API request for
    every website URL.
    """

    all_rows = []
    start_row = 0

    while True:
        request_body = {
            "startDate": str(start_date),
            "endDate": str(end_date),
            "dimensions": [
                "date",
                "page",
                "query",
            ],
            "type": "web",
            "dataState": "final",
            "aggregationType": "auto",
            "rowLimit": (
                SEARCH_CONSOLE_ROW_LIMIT
            ),
            "startRow": start_row,
        }

        response = (
            service.searchanalytics()
            .query(
                siteUrl=(
                    SEARCH_CONSOLE_SITE_URL
                ),
                body=request_body,
            )
            .execute()
        )

        rows = response.get(
            "rows",
            [],
        )

        all_rows.extend(rows)

        print(
            "Search Console page:"
            f" startRow={start_row}"
            f" | rows={len(rows)}"
        )

        if len(rows) < SEARCH_CONSOLE_ROW_LIMIT:
            break

        start_row += SEARCH_CONSOLE_ROW_LIMIT

    return all_rows


def extract_search_console_values(row):
    keys = row.get(
        "keys",
        [],
    )

    if len(keys) < 3:
        raise ValueError(
            "Search Console row is missing "
            "date, page or query dimensions."
        )

    metric_date = datetime.strptime(
        keys[0],
        "%Y-%m-%d",
    ).date()

    page = normalize_content_url(
        keys[1]
    )

    query = str(
        keys[2] or ""
    ).strip()

    if not page:
        raise ValueError(
            "Search Console row has no page URL."
        )

    return {
        "metric_date": metric_date,
        "page_url": page,
        "query": query,
        "clicks": clean_int(
            row.get("clicks")
        ),
        "impressions": clean_int(
            row.get("impressions")
        ),
        "ctr": clean_float(
            row.get("ctr")
        ),
        "average_position": clean_float(
            row.get("position")
        ),
    }


def save_search_console_row(values):
    """
    Connect the Search Console result to the master
    Content record and store the daily query metric.
    """

    with transaction.atomic():
        content, _ = (
            get_or_create_website_content(
                url=values["page_url"],
                source="search_console_fallback",
            )
        )

        analytics_record, created = (
            SearchConsoleAnalytics.objects.update_or_create(
                metric_date=values["metric_date"],
                page_url=values["page_url"],
                query=values["query"],
                defaults={
                    "content": content,
                    "clicks": values["clicks"],
                    "impressions": values[
                        "impressions"
                    ],
                    "ctr": values["ctr"],
                    "average_position": values[
                        "average_position"
                    ],
                },
            )
        )

    return analytics_record, created


def collect_search_console_to_database():
    """
    Main Search Console database collection function.
    """

    log = SystemLog.objects.create(
        module="search_console_collector",
        status="running",
        records_processed=0,
    )

    processed_count = 0
    created_count = 0
    updated_count = 0
    failed_rows = []

    try:
        service = (
            create_search_console_service()
        )

        permission_level = (
            validate_property_access(service)
        )

        print(
            "Search Console property:"
            f" {SEARCH_CONSOLE_SITE_URL}"
        )

        print(
            "Permission level:"
            f" {permission_level}"
        )

        end_date = (
            timezone.localdate()
            - timedelta(
                days=(
                    SEARCH_CONSOLE_END_LAG_DAYS
                )
            )
        )

        start_date = (
            end_date
            - timedelta(
                days=(
                    SEARCH_CONSOLE_LOOKBACK_DAYS
                    - 1
                )
            )
        )

        print(
            "Collecting Search Console data:"
            f" {start_date} to {end_date}"
        )

        response_rows = (
            fetch_search_console_rows(
                service,
                start_date,
                end_date,
            )
        )

        print(
            "Total Search Console rows:"
            f" {len(response_rows)}"
        )

        for index, row in enumerate(
            response_rows,
            start=1,
        ):
            try:
                values = (
                    extract_search_console_values(
                        row
                    )
                )

                _, created = (
                    save_search_console_row(
                        values
                    )
                )

                processed_count += 1

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                if index % 500 == 0:
                    print(
                        "Saved Search Console rows:"
                        f" {index}"
                    )

            except Exception as row_error:
                failed_rows.append(
                    str(row_error)
                )

                print(
                    "Search Console row failed:"
                    f" {row_error}"
                )

        if failed_rows:
            final_status = "warning"

            error_message = "\n".join(
                failed_rows[:30]
            )
        else:
            final_status = "success"
            error_message = ""

        log.status = final_status
        log.records_processed = (
            processed_count
        )
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

        return {
            "processed": processed_count,
            "created": created_count,
            "updated": updated_count,
            "failed": len(failed_rows),
            "start_date": start_date,
            "end_date": end_date,
            "property": (
                SEARCH_CONSOLE_SITE_URL
            ),
        }

    except HttpError as error:
        log.status = "failed"
        log.records_processed = (
            processed_count
        )
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

    except Exception as error:
        log.status = "failed"
        log.records_processed = (
            processed_count
        )
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