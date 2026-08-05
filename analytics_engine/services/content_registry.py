import hashlib
import re
from urllib.parse import urlparse, urlunparse

from django.db import transaction
from django.db.models import Q

from analytics_engine.models import Content


FALLBACK_SOURCES = {
    "",
    "ga4_fallback",
    "mailerlite_fallback",
    "search_console_fallback",
}


def normalize_content_url(url):
    """
    Create one stable URL format for discovery,
    GA4 and Search Console matching.

    Removes:
    - query strings
    - fragments
    - unnecessary trailing slash
    """

    if not url:
        return ""

    raw_url = str(url).strip()

    if not raw_url:
        return ""

    parsed = urlparse(raw_url)

    scheme = parsed.scheme.lower() or "https"
    hostname = parsed.netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    path = parsed.path or "/"

    if path != "/":
        path = path.rstrip("/")

    normalized = urlunparse(
        (
            scheme,
            hostname,
            path,
            "",
            "",
            "",
        )
    )

    return normalized


def get_url_path(url):
    normalized_url = normalize_content_url(url)

    if not normalized_url:
        return "/"

    return urlparse(normalized_url).path or "/"


def create_website_content_id(url):
    normalized_url = normalize_content_url(url)

    digest = hashlib.md5(
        normalized_url.encode("utf-8")
    ).hexdigest()[:12].upper()

    return f"WEB_{digest}"


def create_mailerlite_content_id(campaign_id):
    return f"MAILERLITE_{campaign_id}"


def guess_content_type(url):
    path = get_url_path(url).lower()

    if "/blog/" in path or path.startswith("/blog"):
        return "blog"

    if "/news/" in path or path.startswith("/news"):
        return "news"

    if "/newsletter/" in path:
        return "newsletter"

    if "/case-study/" in path:
        return "case_study"

    return "page"


def guess_title_from_url(url):
    path = get_url_path(url)

    if path == "/":
        return "Medvolt"

    slug = path.rstrip("/").split("/")[-1]

    slug = re.sub(
        r"[-_]+",
        " ",
        slug,
    )

    return slug.strip().title() or "Untitled Page"


def update_existing_content(
    content,
    *,
    title=None,
    publish_date=None,
    content_type=None,
    source=None,
):
    """
    Update useful metadata without overwriting good
    discovery information with fallback information.
    """

    changed_fields = []

    if title and (
        not content.title
        or content.title == "Untitled Page"
        or content.source in FALLBACK_SOURCES
    ):
        content.title = title
        changed_fields.append("title")

    if publish_date and not content.publish_date:
        content.publish_date = publish_date
        changed_fields.append("publish_date")

    if content_type and (
        not content.content_type
        or content.content_type == "page"
    ):
        content.content_type = content_type
        changed_fields.append("content_type")

    if (
        source
        and source == "content_discovery"
        and content.source in FALLBACK_SOURCES
    ):
        content.source = source
        changed_fields.append("source")

    if changed_fields:
        changed_fields.append("updated_at")

        content.save(
            update_fields=list(set(changed_fields))
        )

    return content


@transaction.atomic
def get_or_create_website_content(
    *,
    url,
    title="",
    publish_date=None,
    content_type=None,
    source="content_discovery",
):
    normalized_url = normalize_content_url(url)

    if not normalized_url:
        raise ValueError(
            "A valid website URL is required."
        )

    page_path = get_url_path(normalized_url)

    existing_content = (
        Content.objects.filter(
            platform="website"
        )
        .filter(
            Q(canonical_url=normalized_url)
            | Q(website_url=normalized_url)
            | Q(external_id=page_path)
        )
        .first()
    )

    resolved_title = (
        title.strip()
        if title and title.strip()
        else guess_title_from_url(normalized_url)
    )

    resolved_content_type = (
        content_type
        or guess_content_type(normalized_url)
    )

    if existing_content:
        update_existing_content(
            existing_content,
            title=resolved_title,
            publish_date=publish_date,
            content_type=resolved_content_type,
            source=source,
        )

        return existing_content, False

    content = Content.objects.create(
        content_id=create_website_content_id(
            normalized_url
        ),
        platform="website",
        content_type=resolved_content_type,
        title=resolved_title,
        website_url=normalized_url,
        canonical_url=normalized_url,
        external_id=page_path,
        publish_date=publish_date,
        topic="",
        author="Medvolt",
        status="published",
        source=source,
    )

    return content, True


@transaction.atomic
def get_or_create_mailerlite_content(
    *,
    campaign_id,
    subject="",
    sent_date=None,
    source="content_discovery",
):
    campaign_id = str(campaign_id).strip()

    if not campaign_id:
        raise ValueError(
            "MailerLite campaign ID is required."
        )

    content_id = create_mailerlite_content_id(
        campaign_id
    )

    existing_content = (
        Content.objects.filter(
            platform="mailerlite"
        )
        .filter(
            Q(external_id=campaign_id)
            | Q(content_id=content_id)
        )
        .first()
    )

    resolved_subject = (
        subject.strip()
        if subject and subject.strip()
        else f"MailerLite Campaign {campaign_id}"
    )

    if existing_content:
        update_existing_content(
            existing_content,
            title=resolved_subject,
            publish_date=sent_date,
            content_type="newsletter",
            source=source,
        )

        return existing_content, False

    content = Content.objects.create(
        content_id=content_id,
        platform="mailerlite",
        content_type="newsletter",
        title=resolved_subject,
        website_url="",
        canonical_url="",
        external_id=campaign_id,
        publish_date=sent_date,
        topic="",
        author="Medvolt",
        status="published",
        source=source,
    )

    return content, True