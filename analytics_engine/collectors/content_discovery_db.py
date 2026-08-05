from datetime import datetime
from xml.etree import ElementTree

import requests
from decouple import config
from django.utils import timezone
from django.utils.dateparse import (
    parse_date,
    parse_datetime,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from analytics_engine.collectors.mailerlite_db_collector import (
    extract_campaign_values,
    fetch_mailerlite_campaigns,
)
from analytics_engine.models import SystemLog
from analytics_engine.services.content_registry import (
    get_or_create_mailerlite_content,
    get_or_create_website_content,
)


WEBSITE_SITEMAP_URL = config(
    "WEBSITE_SITEMAP_URL",
    default="https://medvolt.ai/sitemap.xml",
).strip()


def create_http_session():
    retry_strategy = Retry(
        total=3,
        connect=3,
        read=3,
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
        max_retries=retry_strategy
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

    session.headers.update(
        {
            "User-Agent": (
                "Medvolt Analytics Content Discovery"
            )
        }
    )

    return session


def convert_to_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    text_value = str(value).strip()

    parsed_datetime = parse_datetime(
        text_value
    )

    if parsed_datetime:
        return parsed_datetime.date()

    parsed_date = parse_date(
        text_value[:10]
    )

    return parsed_date


def find_child_text(element, child_name):
    for child in element:
        if child.tag.endswith(child_name):
            return (
                child.text.strip()
                if child.text
                else ""
            )

    return ""


def read_sitemap(
    sitemap_url,
    session,
    visited=None,
):
    """
    Supports both:

    sitemap index:
    <sitemapindex>

    normal sitemap:
    <urlset>
    """

    if visited is None:
        visited = set()

    if sitemap_url in visited:
        return []

    visited.add(sitemap_url)

    response = session.get(
        sitemap_url,
        timeout=30,
    )

    response.raise_for_status()

    root = ElementTree.fromstring(
        response.content
    )

    discovered_pages = []

    if root.tag.endswith("sitemapindex"):
        for sitemap_element in root:
            child_sitemap_url = find_child_text(
                sitemap_element,
                "loc",
            )

            if not child_sitemap_url:
                continue

            discovered_pages.extend(
                read_sitemap(
                    child_sitemap_url,
                    session,
                    visited,
                )
            )

        return discovered_pages

    if root.tag.endswith("urlset"):
        for url_element in root:
            page_url = find_child_text(
                url_element,
                "loc",
            )

            last_modified = find_child_text(
                url_element,
                "lastmod",
            )

            if not page_url:
                continue

            discovered_pages.append(
                {
                    "url": page_url,
                    "last_modified": (
                        convert_to_date(
                            last_modified
                        )
                    ),
                }
            )

    return discovered_pages


def discover_website_content():
    session = create_http_session()

    sitemap_pages = read_sitemap(
        WEBSITE_SITEMAP_URL,
        session,
    )

    created_count = 0
    updated_count = 0
    failed_count = 0
    errors = []

    for page in sitemap_pages:
        try:
            _, created = (
                get_or_create_website_content(
                    url=page["url"],
                    publish_date=page[
                        "last_modified"
                    ],
                    source="content_discovery",
                )
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        except Exception as error:
            failed_count += 1
            errors.append(
                f"{page['url']}: {error}"
            )

    return {
        "found": len(sitemap_pages),
        "created": created_count,
        "updated": updated_count,
        "failed": failed_count,
        "errors": errors,
    }


def discover_mailerlite_content():
    
    campaigns = list(
        fetch_mailerlite_campaigns()
        )
    

    created_count = 0
    updated_count = 0
    failed_count = 0
    errors = []

    for campaign in campaigns:
        try:
            values = extract_campaign_values(
                campaign
            )

            _, created = (
                get_or_create_mailerlite_content(
                    campaign_id=values[
                        "campaign_id"
                    ],
                    subject=values["subject"],
                    sent_date=values[
                        "sent_date"
                    ],
                    source="content_discovery",
                )
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        except Exception as error:
            failed_count += 1
            errors.append(str(error))

    return {
        "found": len(campaigns),
        "created": created_count,
        "updated": updated_count,
        "failed": failed_count,
        "errors": errors,
    }


def discover_content_to_database():
    log = SystemLog.objects.create(
        module="content_discovery",
        status="running",
        records_processed=0,
    )

    try:
        website_result = (
            discover_website_content()
        )

        mailerlite_result = (
            discover_mailerlite_content()
        )

        total_processed = (
            website_result["found"]
            + mailerlite_result["found"]
        )

        total_failed = (
            website_result["failed"]
            + mailerlite_result["failed"]
        )

        all_errors = (
            website_result["errors"]
            + mailerlite_result["errors"]
        )

        log.status = (
            "warning"
            if total_failed
            else "success"
        )

        log.records_processed = total_processed
        log.error_message = "\n".join(
            all_errors[:30]
        )

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
            "website": website_result,
            "mailerlite": mailerlite_result,
            "processed": total_processed,
            "failed": total_failed,
        }

    except Exception as error:
        log.status = "failed"
        log.error_message = str(error)
        log.completed_at = timezone.now()

        log.save(
            update_fields=[
                "status",
                "error_message",
                "completed_at",
            ]
        )

        raise