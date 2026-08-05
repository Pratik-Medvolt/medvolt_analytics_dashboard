import requests
from decouple import config
from datetime import datetime

from analytics_engine.processors.normalize import safe_date


MAILERLITE_API_KEY = config("MAILERLITE_API_KEY", default="")


def discover_mailerlite_campaigns():
    if not MAILERLITE_API_KEY:
        print("MailerLite API key missing. Skipping MailerLite discovery.")
        return []

    url = "https://connect.mailerlite.com/api/campaigns"

    headers = {
        "Authorization": f"Bearer {MAILERLITE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    records = []
    page = 1

    while True:
        response = requests.get(
            url,
            headers=headers,
            params={"page": page, "limit": 100},
            timeout=30,
        )

        if response.status_code != 200:
            print(f"MailerLite API error: {response.status_code} | {response.text}")
            break

        payload = response.json()
        campaigns = payload.get("data", [])

        if not campaigns:
            break

        for campaign in campaigns:
            campaign_id = str(campaign.get("id", ""))

            subject = (
                campaign.get("name")
                or campaign.get("subject")
                or campaign.get("settings", {}).get("subject")
                or "Untitled Newsletter"
            )

            sent_at = (
                campaign.get("sent_at")
                or campaign.get("created_at")
                or datetime.now().isoformat()
            )

            status = campaign.get("status", "published")

            records.append({
                "platform": "mailerlite",
                "type": "newsletter",
                "title": subject,
                "url": "",
                "external_id": campaign_id,
                "publish_date": safe_date(sent_at),
                "topic": "Newsletter",
                "author": "Medvolt",
                "status": status,
                "source": "mailerlite_api",
            })

        meta = payload.get("meta", {})
        current_page = meta.get("current_page", page)
        last_page = meta.get("last_page", page)

        if current_page >= last_page:
            break

        page += 1

    print(f"MailerLite records found: {len(records)}")
    return records