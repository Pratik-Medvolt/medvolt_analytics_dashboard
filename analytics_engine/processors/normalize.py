from urllib.parse import urlparse, urlunparse
from datetime import datetime
import hashlib


def normalize_url(url):
    if not url:
        return ""

    parsed = urlparse(url.strip())

    clean = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
        "",
        "",
        ""
    ))

    return clean


def safe_date(value):
    if not value:
        return datetime.now().date().isoformat()

    return str(value).split("T")[0]


def create_content_id(platform, content_type, unique_value):
    base = f"{platform}_{content_type}_{unique_value}"
    digest = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    date_part = datetime.now().strftime("%Y%m%d")

    return f"{platform.upper()}_{content_type.upper()}_{date_part}_{digest}"