from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from decouple import config
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from google.analytics.data_v1beta import (
    BetaAnalyticsDataClient,
)
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.oauth2.service_account import Credentials

from analytics_engine.models import (
    SystemLog,
    WebsiteAnalytics,
)
from analytics_engine.services.content_registry import (
    get_or_create_website_content,
)


GA4_PROPERTY_ID = config(
    "GA4_PROPERTY_ID",
    default="",
).strip()

GOOGLE_CREDENTIALS_FILE = config(
    "GOOGLE_CREDENTIALS_FILE",
    default="google_credentials.json",
).strip()

WEBSITE_BASE_URL = config(
    "WEBSITE_BASE_URL",
    default="https://medvolt.ai",
).strip().rstrip("/")

GA4_LOOKBACK_DAYS = config(
    "GA4_LOOKBACK_DAYS",
    default=90,
    cast=int,
)

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
]


def clean_int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def clean_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_page_path(value):
    """
    Convert a GA4 page path into a stable path.
    """

    if not value:
        return "/"

    parsed = urlparse(
        str(value).strip()
    )

    path = parsed.path or "/"

    if path != "/":
        path = path.rstrip("/")

    return path.lower()


def build_page_url(page_path):
    """
    Convert a page path into a full website URL.
    """

    normalized_path = normalize_page_path(
        page_path
    )

    if normalized_path == "/":
        return f"{WEBSITE_BASE_URL}/"

    return urljoin(
        f"{WEBSITE_BASE_URL}/",
        normalized_path.lstrip("/"),
    )


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


def create_ga4_client():
    if not GA4_PROPERTY_ID:
        raise ValueError(
            "GA4_PROPERTY_ID is missing from .env"
        )

    credentials = (
        Credentials.from_service_account_file(
            str(resolve_credentials_path()),
            scopes=SCOPES,
        )
    )

    return BetaAnalyticsDataClient(
        credentials=credentials
    )


def parse_ga4_date(value):
    return datetime.strptime(
        value,
        "%Y%m%d",
    ).date()


def fetch_ga4_report_rows(
    client,
    start_date,
    end_date,
):
    """
    Fetch daily GA4 page and source analytics.
    """

    request = RunReportRequest(
        property=(
            f"properties/{GA4_PROPERTY_ID}"
        ),
        dimensions=[
            Dimension(name="date"),
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
            Dimension(name="sessionSource"),
        ],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="sessions"),
            Metric(name="totalUsers"),
            Metric(
                name="userEngagementDuration"
            ),
        ],
        date_ranges=[
            DateRange(
                start_date=str(start_date),
                end_date=str(end_date),
            )
        ],
        limit=100000,
    )

    response = client.run_report(
        request
    )

    return list(response.rows)


def extract_ga4_values(row):
    metric_date = parse_ga4_date(
        row.dimension_values[0].value
    )

    page_path = normalize_page_path(
        row.dimension_values[1].value
    )

    page_title = (
        row.dimension_values[2].value
        or page_path
    ).strip()

    traffic_source = (
        row.dimension_values[3].value
        or "(not set)"
    ).strip()

    return {
        "metric_date": metric_date,
        "page_path": page_path,
        "page_url": build_page_url(
            page_path
        ),
        "page_title": page_title,
        "traffic_source": traffic_source,
        "views": clean_int(
            row.metric_values[0].value
        ),
        "sessions": clean_int(
            row.metric_values[1].value
        ),
        "users": clean_int(
            row.metric_values[2].value
        ),
        "engagement_time_seconds": (
            clean_float(
                row.metric_values[3].value
            )
        ),
    }


def aggregate_ga4_rows(response_rows):
    """
    Combine GA4 rows that resolve to the same
    database identity.

    GA4 includes pageTitle as a dimension, but the
    database identity is:

    metric_date + page_url + traffic_source

    Without aggregation, page-title variations can
    overwrite each other during collection.
    """

    aggregated = {}
    extraction_errors = []

    for index, row in enumerate(
        response_rows,
        start=1,
    ):
        try:
            values = extract_ga4_values(
                row
            )

            key = (
                values["metric_date"],
                values["page_url"],
                values["traffic_source"],
            )

            if key not in aggregated:
                aggregated[key] = values
                continue

            existing = aggregated[key]

            existing["views"] += (
                values["views"]
            )

            existing["sessions"] += (
                values["sessions"]
            )

            existing["users"] += (
                values["users"]
            )

            existing[
                "engagement_time_seconds"
            ] += values[
                "engagement_time_seconds"
            ]

            if (
                not existing["page_title"]
                or existing["page_title"]
                == existing["page_path"]
            ):
                existing["page_title"] = (
                    values["page_title"]
                )

        except Exception as error:
            extraction_errors.append(
                f"Row {index}: {error}"
            )

    return (
        list(aggregated.values()),
        extraction_errors,
    )


def save_ga4_row(values):
    """
    Save one already-aggregated GA4 row.

    The outer collection function controls the
    database transaction.
    """

    content, _ = (
        get_or_create_website_content(
            url=values["page_url"],
            title=values["page_title"],
            source="ga4_fallback",
        )
    )

    analytics_record, created = (
        WebsiteAnalytics.objects
        .update_or_create(
            metric_date=values[
                "metric_date"
            ],
            page_url=values["page_url"],
            traffic_source=values[
                "traffic_source"
            ],
            defaults={
                "content": content,
                "page_title": values[
                    "page_title"
                ],
                "views": values["views"],
                "sessions": values[
                    "sessions"
                ],
                "users": values["users"],
                "engagement_time_seconds": (
                    values[
                        "engagement_time_seconds"
                    ]
                ),
            },
        )
    )

    return analytics_record, created


def collect_ga4_to_database():
    """
    Fetch the complete GA4 report first, then refresh
    the database inside one atomic transaction.
    """

    log = SystemLog.objects.create(
        module="ga4_collector",
        status="running",
        records_processed=0,
    )

    processed_count = 0
    created_count = 0
    updated_count = 0
    stale_deleted_count = 0

    try:
        client = create_ga4_client()

        end_date = (
            timezone.localdate()
            - timedelta(days=1)
        )

        start_date = (
            end_date
            - timedelta(
                days=GA4_LOOKBACK_DAYS - 1
            )
        )

        print(
            "Collecting GA4 data from "
            f"{start_date} to {end_date}"
        )

        response_rows = (
            fetch_ga4_report_rows(
                client,
                start_date,
                end_date,
            )
        )

        print(
            "Raw GA4 response rows: "
            f"{len(response_rows)}"
        )

        prepared_rows, extraction_errors = (
            aggregate_ga4_rows(
                response_rows
            )
        )

        if extraction_errors:
            error_preview = "\n".join(
                extraction_errors[:20]
            )

            raise ValueError(
                "GA4 response preparation failed "
                f"for {len(extraction_errors)} "
                "rows.\n"
                f"{error_preview}"
            )

        print(
            "Aggregated GA4 database rows: "
            f"{len(prepared_rows)}"
        )

        with transaction.atomic():
            existing_ids = set(
                WebsiteAnalytics.objects
                .filter(
                    metric_date__range=(
                        start_date,
                        end_date,
                    )
                )
                .values_list(
                    "id",
                    flat=True,
                )
            )

            saved_ids = set()

            for index, values in enumerate(
                prepared_rows,
                start=1,
            ):
                analytics_record, created = (
                    save_ga4_row(values)
                )

                saved_ids.add(
                    analytics_record.id
                )

                processed_count += 1

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                if index % 250 == 0:
                    print(
                        "Saved GA4 rows: "
                        f"{index}/"
                        f"{len(prepared_rows)}"
                    )

            stale_ids = (
                existing_ids
                - saved_ids
            )

            if stale_ids:
                deleted_result = (
                    WebsiteAnalytics.objects
                    .filter(id__in=stale_ids)
                    .delete()
                )

                stale_deleted_count = (
                    deleted_result[0]
                )

            log.status = "success"
            log.records_processed = (
                processed_count
            )
            log.error_message = ""
            log.completed_at = (
                timezone.now()
            )

            log.save(
                update_fields=[
                    "status",
                    "records_processed",
                    "error_message",
                    "completed_at",
                ]
            )

        print(
            "GA4 database refresh committed."
        )

        print(
            "Stale GA4 rows removed: "
            f"{stale_deleted_count}"
        )

        return {
            "processed": processed_count,
            "created": created_count,
            "updated": updated_count,
            "failed": 0,
            "stale_deleted": (
                stale_deleted_count
            ),
            "raw_rows": len(
                response_rows
            ),
            "aggregated_rows": len(
                prepared_rows
            ),
            "start_date": start_date,
            "end_date": end_date,
        }

    except Exception as error:
        log.status = "failed"
        log.records_processed = (
            processed_count
        )
        log.error_message = str(error)
        log.completed_at = (
            timezone.now()
        )

        log.save(
            update_fields=[
                "status",
                "records_processed",
                "error_message",
                "completed_at",
            ]
        )

        raise