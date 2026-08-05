import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from decouple import config
from datetime import datetime

from analytics_engine.processors.normalize import normalize_url, safe_date


SITEMAP_URL = config("WEBSITE_SITEMAP_URL", default="")


def should_skip_url(url):
    url = url.lower()

    skip_patterns = [
        "substack.com",
        "/blog",
        "/p/",
        "?utm_",
    ]

    return any(pattern in url for pattern in skip_patterns)


def fetch_xml(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.content


def parse_sitemap(xml_content):
    root = ET.fromstring(xml_content)
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

    sitemap_urls = []
    page_urls = []

    if root.tag.endswith("sitemapindex"):
        for sitemap in root.findall(f"{namespace}sitemap"):
            loc = sitemap.find(f"{namespace}loc")
            if loc is not None and loc.text:
                sitemap_urls.append(loc.text.strip())

    elif root.tag.endswith("urlset"):
        for url_tag in root.findall(f"{namespace}url"):
            loc = url_tag.find(f"{namespace}loc")
            lastmod = url_tag.find(f"{namespace}lastmod")

            if loc is not None and loc.text:
                page_urls.append({
                    "url": loc.text.strip(),
                    "lastmod": lastmod.text.strip() if lastmod is not None and lastmod.text else "",
                })

    return sitemap_urls, page_urls


def get_all_sitemap_pages():
    if not SITEMAP_URL:
        print("Website sitemap URL missing. Skipping sitemap discovery.")
        return []

    xml_content = fetch_xml(SITEMAP_URL)
    sitemap_urls, page_urls = parse_sitemap(xml_content)

    all_pages = page_urls[:]

    for sitemap_url in sitemap_urls:
        try:
            child_xml = fetch_xml(sitemap_url)
            _, child_pages = parse_sitemap(child_xml)
            all_pages.extend(child_pages)
        except Exception as e:
            print(f"Failed child sitemap: {sitemap_url} | {e}")

    return all_pages


def detect_content_type(url):
    url = url.lower()

    if "/whitepaper" in url or "/white-paper" in url:
        return "whitepaper"

    if "/event" in url or "/conference" in url:
        return "event"

    if "/case-study" in url or "/case-studies" in url:
        return "case_study"

    if "/product" in url:
        return "product_page"

    return "website_page"


def fetch_page_title(url):
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        if soup.title and soup.title.text:
            return soup.title.text.strip()

        h1 = soup.find("h1")
        if h1:
            return h1.text.strip()

        return "Untitled Page"

    except Exception as e:
        print(f"Failed title fetch: {url} | {e}")
        return "Untitled Page"


def discover_website_content():
    pages = get_all_sitemap_pages()
    records = []

    for item in pages:
        url = normalize_url(item["url"])

        if should_skip_url(url):
            continue

        content_type = detect_content_type(url)
        title = fetch_page_title(url)

        records.append({
            "platform": "website",
            "type": content_type,
            "title": title,
            "website_url": url,
            "substack_url": "",
            "canonical_url": url,
            "external_id": url,
            "publish_date": safe_date(item.get("lastmod")),
            "topic": "",
            "author": "Medvolt",
            "status": "published",
            "source": "sitemap",
            })

    print(f"Website sitemap records found: {len(records)}")
    return records