import feedparser
from decouple import config
from datetime import datetime

from analytics_engine.processors.normalize import normalize_url


FEED_URL = config("SUBSTACK_FEED_URL", default="")


def discover_substack_posts():
    if not FEED_URL:
        print("Substack feed URL missing. Skipping Substack discovery.")
        return []

    feed = feedparser.parse(FEED_URL)

    records = []

    for entry in feed.entries:
        title = entry.get("title", "Untitled")
        url = normalize_url(entry.get("link", ""))
        author = entry.get("author", "Medvolt")

        try:
            publish_date = datetime(*entry.published_parsed[:6]).date().isoformat()
        except Exception:
            publish_date = datetime.now().date().isoformat()

        records.append({
            "platform": "multi",
            "type": "blog",
            "title": title,
            "website_url": "",
            "substack_url": url,
            "canonical_url": "",
            "external_id": url,
            "publish_date": publish_date,
            "topic": "",
            "author": author,
            "status": "published",
            "source": "substack_rss",
            })

    print(f"Substack records found: {len(records)}")
    return records