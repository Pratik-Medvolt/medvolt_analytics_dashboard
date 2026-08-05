from datetime import datetime

from analytics_engine.discover.substack_discovery import discover_substack_posts
from analytics_engine.discover.sitemap_discovery import discover_website_content
from analytics_engine.discover.mailerlite_discovery import discover_mailerlite_campaigns
from analytics_engine.discover.linkedin_discovery import discover_linkedin_posts
from analytics_engine.discover.x_discovery import discover_x_posts

from analytics_engine.sheets.sheets_client import read_content_library, append_content_rows
from analytics_engine.processors.normalize import normalize_url, create_content_id


def build_existing_keys(existing_rows):
    keys = set()

    for row in existing_rows:
        website_url = normalize_url(row.get("website_url", ""))
        substack_url = normalize_url(row.get("substack_url", ""))
        canonical_url = normalize_url(row.get("canonical_url", ""))

        external_id = str(row.get("external_id", "")).strip()
        platform = str(row.get("platform", "")).strip().lower()
        title = str(row.get("title", "")).strip().lower()
        publish_date = str(row.get("publish_date", "")).strip()

        if website_url:
            keys.add(f"website_url::{website_url}")

        if substack_url:
            keys.add(f"substack_url::{substack_url}")

        if canonical_url:
            keys.add(f"canonical_url::{canonical_url}")

        if external_id:
            keys.add(f"external::{platform}::{external_id}")

        if title and publish_date and platform:
            keys.add(f"title_date::{platform}::{title}::{publish_date}")

    return keys


def is_duplicate(record, existing_keys):
    url = normalize_url(
        record.get("website_url")
        or record.get("substack_url")
        or ""
        )
    external_id = str(record.get("external_id", "")).strip()
    platform = str(record.get("platform", "")).strip().lower()
    title = str(record.get("title", "")).strip().lower()
    publish_date = str(record.get("publish_date", "")).strip()

    if url and f"url::{url}" in existing_keys:
        return True

    if external_id and f"external::{platform}::{external_id}" in existing_keys:
        return True

    if title and publish_date and platform:
        if f"title_date::{platform}::{title}::{publish_date}" in existing_keys:
            return True

    return False


def record_to_row(record):
    unique_value = (
        record.get("url")
        or record.get("external_id")
        or record.get("title")
        or datetime.now().isoformat()
    )

    content_id = create_content_id(
        record.get("platform", "unknown"),
        record.get("type", "content"),
        unique_value,
    )

    return [
        content_id,
        record.get("platform", ""),
        record.get("type", ""),
        record.get("title", ""),
        record.get("website_url", ""),
        record.get("substack_url", ""),
        record.get("canonical_url", ""),
        record.get("external_id", ""),
        record.get("publish_date", ""),
        record.get("topic", ""),
        record.get("author", "Medvolt"),
        record.get("status", "published"),
        record.get("source", ""),
        datetime.now().isoformat(),
        ]


def main():
    print("\nStarting Universal Content Discovery...\n")

    existing_rows = read_content_library()
    existing_keys = build_existing_keys(existing_rows)

    discovered_records = []

    discovered_records.extend(discover_substack_posts())
    discovered_records.extend(discover_website_content())
    discovered_records.extend(discover_mailerlite_campaigns())
    discovered_records.extend(discover_linkedin_posts())
    discovered_records.extend(discover_x_posts())

    rows_to_insert = []
    session_keys = set()

    for record in discovered_records:
        if is_duplicate(record, existing_keys):
            continue

        url = normalize_url(record.get("url", ""))
        external_id = str(record.get("external_id", "")).strip()
        platform = str(record.get("platform", "")).strip().lower()

        session_key = url or f"{platform}::{external_id}"

        if session_key in session_keys:
            continue

        rows_to_insert.append(record_to_row(record))
        session_keys.add(session_key)

    append_content_rows(rows_to_insert)

    print(f"\nTotal discovered records: {len(discovered_records)}")
    print(f"New records inserted: {len(rows_to_insert)}")
    print("Content discovery completed.\n")


if __name__ == "__main__":
    main()