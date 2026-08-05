import requests

from decouple import config
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from analytics_engine.services.content_registry import (
    get_or_create_mailerlite_content,
)

from analytics_engine.models import (
    MailerLiteAnalytics,
    SystemLog,
)


MAILERLITE_API_KEY = config(
    "MAILERLITE_API_KEY",
    default="",
)

MAILERLITE_CAMPAIGNS_URL = (
    "https://connect.mailerlite.com/api/campaigns"
)


def clean_int(value):
    """
    Convert a MailerLite value into an integer.
    """

    try:
        if isinstance(value, dict):
            for key in (
                "value",
                "count",
                "integer",
                "float",
            ):
                if key in value:
                    value = value[key]
                    break

        return int(float(value or 0))

    except (TypeError, ValueError):
        return 0


def clean_rate(value):
    """
    Convert a MailerLite rate into a percentage.

    Examples:
    0.235  -> 23.5
    23.5   -> 23.5
    23.5%  -> 23.5
    """

    try:
        if isinstance(value, dict):

            if "string" in value:
                raw_value = str(
                    value["string"]
                ).replace("%", "").strip()

                return round(float(raw_value), 2)

            if "float" in value:
                raw_value = float(value["float"])

                if raw_value <= 1:
                    raw_value *= 100

                return round(raw_value, 2)

            if "value" in value:
                raw_value = float(value["value"])

                if raw_value <= 1:
                    raw_value *= 100

                return round(raw_value, 2)

            return 0

        if isinstance(value, str):
            raw_value = float(
                value.replace("%", "").strip()
            )

            if raw_value <= 1:
                raw_value *= 100

            return round(raw_value, 2)

        if isinstance(value, (int, float)):
            raw_value = float(value)

            if raw_value <= 1:
                raw_value *= 100

            return round(raw_value, 2)

        return 0

    except (TypeError, ValueError):
        return 0


def get_nested_value(
    data,
    possible_keys,
    default=0,
):
    """
    Check multiple possible field paths.

    Example:
    unique_opens_count
    total.opens
    """

    for key in possible_keys:
        value = data

        try:
            for part in key.split("."):
                if not isinstance(value, dict):
                    value = None
                    break

                value = value.get(part)

            if value is not None:
                return value

        except (TypeError, AttributeError):
            continue

    return default


def convert_to_date(value):
    """
    Convert an API date or datetime into a Python date.
    """

    if not value:
        return None

    value = str(value).strip()

    parsed_datetime = parse_datetime(value)

    if parsed_datetime:
        return parsed_datetime.date()

    parsed_date = parse_date(value)

    if parsed_date:
        return parsed_date

    if len(value) >= 10:
        return parse_date(value[:10])

    return None


def create_http_session():
    """
    Create a requests session with retry support.
    """

    retry_strategy = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy,
    )

    session = requests.Session()

    session.mount(
        "https://",
        adapter,
    )

    session.mount(
        "http://",
        adapter,
    )

    return session


def extract_campaign_values(campaign):
    """
    Extract the required values from one campaign.
    """

    campaign_id = str(
        campaign.get("id", "")
    ).strip()

    settings = campaign.get("settings") or {}

    subject = (
        campaign.get("name")
        or campaign.get("subject")
        or settings.get("subject")
        or "Untitled Newsletter"
    )

    sent_date_value = (
        campaign.get("started_at")
        or campaign.get("sent_at")
        or campaign.get("finished_at")
        or campaign.get("scheduled_for")
        or campaign.get("created_at")
    )
    sent_date = convert_to_date(sent_date_value)

    stats = (
        campaign.get("stats")
        or campaign.get("analytics")
        or campaign.get("report")
        or {}
    )

    opens_raw = get_nested_value(
        stats,
        [
            "unique_opens_count",
            "opens_count",
            "opens",
        ],
        0,
    )

    clicks_raw = get_nested_value(
        stats,
        [
            "unique_clicks_count",
            "clicks_count",
            "clicks",
        ],
        0,
    )

    unsubscribes_raw = get_nested_value(
        stats,
        [
            "unsubscribes_count",
            "unsubscribes",
        ],
        0,
    )

    open_rate_raw = get_nested_value(
        stats,
        [
            "open_rate",
            "opens_rate",
        ],
        0,
    )

    click_rate_raw = get_nested_value(
        stats,
        [
            "click_rate",
            "clicks_rate",
        ],
        0,
    )

    return {
        "campaign_id": campaign_id,
        "subject": str(subject).strip(),
        "sent_date": sent_date,
        "opens": clean_int(opens_raw),
        "open_rate": clean_rate(open_rate_raw),
        "clicks": clean_int(clicks_raw),
        "click_rate": clean_rate(click_rate_raw),
        "unsubscribes": clean_int(
            unsubscribes_raw
        ),
    }


def save_campaign_to_database(
    campaign_values,
    snapshot_date,
):
    """
    Connect a MailerLite campaign to the master
    Content registry and save its analytics snapshot.
    """

    with transaction.atomic():
        content, _ = (
            get_or_create_mailerlite_content(
                campaign_id=campaign_values[
                    "campaign_id"
                ],
                subject=campaign_values[
                    "subject"
                ],
                sent_date=campaign_values[
                    "sent_date"
                ],
                source="mailerlite_fallback",
            )
        )

        analytics_record, created = (
            MailerLiteAnalytics.objects.update_or_create(
                campaign_id=campaign_values[
                    "campaign_id"
                ],
                snapshot_date=snapshot_date,
                defaults={
                    "content": content,
                    "subject": campaign_values[
                        "subject"
                    ],
                    "sent_date": campaign_values[
                        "sent_date"
                    ],
                    "opens": campaign_values[
                        "opens"
                    ],
                    "open_rate": campaign_values[
                        "open_rate"
                    ],
                    "clicks": campaign_values[
                        "clicks"
                    ],
                    "click_rate": campaign_values[
                        "click_rate"
                    ],
                    "unsubscribes": campaign_values[
                        "unsubscribes"
                    ],
                },
            )
        )

    return analytics_record, created


def fetch_mailerlite_campaigns():
    """
    Retrieve all sent campaigns from MailerLite.
    """

    if not MAILERLITE_API_KEY:
        raise ValueError(
            "MAILERLITE_API_KEY is missing from .env"
        )

    headers = {
        "Authorization": (
            f"Bearer {MAILERLITE_API_KEY}"
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    session = create_http_session()

    page = 1

    while True:

        response = session.get(
            MAILERLITE_CAMPAIGNS_URL,
            headers=headers,
            params={
                "page": page,
                "limit": 100,
                "filter[status]": "sent",
            },
            timeout=30,
        )

        response.raise_for_status()

        payload = response.json()

        campaigns = payload.get("data", [])

        if not campaigns:
            break

        for campaign in campaigns:
            yield campaign

        meta = payload.get("meta", {})

        current_page = int(
            meta.get("current_page", page)
        )

        last_page = int(
            meta.get("last_page", page)
        )

        if current_page >= last_page:
            break

        page += 1


def collect_mailerlite_to_database():
    """
    Main function used by the management command
    and later by Celery automation.
    """

    log = SystemLog.objects.create(
        module="mailerlite_collector",
        status="running",
        records_processed=0,
    )

    snapshot_date = timezone.localdate()

    processed_count = 0
    created_count = 0
    updated_count = 0
    failed_campaigns = []

    try:

        for campaign in fetch_mailerlite_campaigns():

            try:
                campaign_values = (
                    extract_campaign_values(campaign)
                )

                _, created = save_campaign_to_database(
                    campaign_values,
                    snapshot_date,
                )

                processed_count += 1

                if created:
                    created_count += 1
                else:
                    updated_count += 1

                print(
                    "Saved:"
                    f" {campaign_values['subject']}"
                    f" | Opens:"
                    f" {campaign_values['opens']}"
                    f" | Clicks:"
                    f" {campaign_values['clicks']}"
                )

            except Exception as campaign_error:
                campaign_id = campaign.get(
                    "id",
                    "unknown",
                )

                failed_campaigns.append(
                    f"{campaign_id}: {campaign_error}"
                )

                print(
                    "Campaign failed:"
                    f" {campaign_id}"
                    f" | {campaign_error}"
                )

        if failed_campaigns:
            final_status = "warning"
            error_message = "\n".join(
                failed_campaigns[:20]
            )
        else:
            final_status = "success"
            error_message = ""

        log.status = final_status
        log.records_processed = processed_count
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
            "failed": len(failed_campaigns),
        }

    except Exception as error:

        log.status = "failed"
        log.records_processed = processed_count
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