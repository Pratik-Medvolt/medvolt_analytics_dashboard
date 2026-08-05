def discover_linkedin_posts():
    """
    LinkedIn is intentionally not implemented in MVP.

    Reason:
    LinkedIn post analytics/content access requires approved LinkedIn API access.
    Do not fake this with scraping. That is fragile and can violate platform rules.

    Later this function should return records in the same format:
    {
        "platform": "linkedin",
        "type": "post",
        "title": "...",
        "url": "...",
        "external_id": "...",
        "publish_date": "...",
        "topic": "...",
        "author": "Medvolt",
        "status": "published",
        "source": "linkedin_api"
    }
    """
    print("LinkedIn discovery skipped. API access not configured.")
    return []