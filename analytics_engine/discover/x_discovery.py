def discover_x_posts():
    """
    X/Twitter is intentionally not implemented in MVP.

    Reason:
    X API access depends on account tier and permissions.
    Do not scrape posts.

    Later this function should return records in the same universal format.
    """
    print("X discovery skipped. API access not configured.")
    return []