import requests
from datetime import datetime
from decouple import config

from analytics_engine.sheets.sheets_client import (
    read_content_library,
    append_mailerlite_rows,
    append_log,
    clear_mailerlite_analytics_sheet,
)

MAILERLITE_API_KEY = config("MAILERLITE_API_KEY", default="")


def clean_int(value):
    try:
        if isinstance(value, dict):
            value = value.get("value", 0)
        return int(float(value or 0))
    except Exception:
        return 0


def clean_rate(value):

    try:
        if isinstance(value, dict):

            if "string" in value:
                string_value = str(value["string"]).replace("%", "").strip()
                return round(float(string_value), 2)

            if "float" in value:
                float_value = float(value["float"])

                # already decimal
                if float_value <= 1:
                    return round(float_value * 100, 2)

                return round(float_value, 2)

            if "value" in value:
                raw = float(value["value"])

                if raw <= 1:
                    return round(raw * 100, 2)

                return round(raw, 2)

            return 0

        if isinstance(value, (int, float)):

            value = float(value)

            if value <= 1:
                return round(value * 100, 2)

            return round(value, 2)

        if isinstance(value, str):
            return round(float(value.replace("%", "").strip()), 2)

        return 0

    except Exception:
        return 0


def get_nested_value(data, possible_keys, default=0):
    """
    Safely checks multiple possible MailerLite response fields.
    """

    for key in possible_keys:
        value = data

        try:
            for part in key.split("."):
                value = value.get(part, {})

            if value != {} and value is not None:
                return value

        except Exception:
            continue

    return default


def get_newsletter_content_map():
    records = read_content_library()
    campaign_map = {}

    for row in records:
        content_type = str(row.get("type", "")).lower()

        if content_type != "newsletter":
            continue

        campaign_id = str(row.get("external_id", "")).strip()

        if not campaign_id:
            continue

        campaign_map[campaign_id] = {
            "content_id": row.get("content_id", ""),
            "title": row.get("title", ""),
            "publish_date": row.get("publish_date", ""),
        }

    return campaign_map


def fetch_mailerlite_analytics():
    if not MAILERLITE_API_KEY:
        print("MailerLite API key missing.")
        append_log("mailerlite_analytics", "failed", 0, "Missing API key")
        return

    campaign_map = get_newsletter_content_map()

    print("Newsletter campaigns in content_library:", len(campaign_map))

    headers = {
        "Authorization": f"Bearer {MAILERLITE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    url = "https://connect.mailerlite.com/api/campaigns"

    rows_to_insert = []
    page = 1

    try:
        while True:
            response = requests.get(
                url,
            headers=headers,
            params={
                "page": page,
                "limit": 100,
                "filter[status]": "sent",
            },
            timeout=30,
        )

            if response.status_code != 200:
                error_message = f"{response.status_code} | {response.text}"
                print("MailerLite API error:", error_message)
                append_log("mailerlite_analytics", "failed", 0, error_message)
                return

            payload = response.json()
            campaigns = payload.get("data", [])

            if not campaigns:
                break

            for campaign in campaigns:
                campaign_id = str(campaign.get("id", "")).strip()

                if campaign_id not in campaign_map:
                    continue

                content_info = campaign_map[campaign_id]

                subject = (
                    campaign.get("name")
                    or campaign.get("subject")
                    or campaign.get("settings", {}).get("subject")
                    or content_info.get("title")
                    or "Untitled Newsletter"
                )

                sent_date = (
                    campaign.get("sent_at")
                    or campaign.get("created_at")
                    or content_info.get("publish_date")
                    or ""
                )

                stats = (
                    campaign.get("stats")
                    or campaign.get("analytics")
                    or campaign.get("report")
                    or {}
                )


                opens = get_nested_value(
                    stats,
                    [
                        "unique_opens_count",
                        "opens_count",
                    ],
                    0,
                )


                clicks = get_nested_value(
                    stats,
                    [
                        "unique_clicks_count",
                        "clicks_count",
                       
                    ],
                    0,
                )

                unsubscribes = get_nested_value(
                    stats,
                    [
                        "unsubscribes_count",
                        
                    ],
                    0,
                )

                open_rate_raw = get_nested_value(
                    stats,
                    [
                        "open_rate",
                        "opens_rate",
                        "rates.open",
                        "rate.opens",
                    ],
                    0,
                )

                click_rate_raw = get_nested_value(
                    stats,
                    [
                        "click_rate",
                        "clicks_rate",
                        "rates.click",
                        "rate.clicks",
                    ],
                    0,
                )

                open_rate = clean_rate(open_rate_raw)
                click_rate = clean_rate(click_rate_raw)

                row = [
                    str(content_info["content_id"]),
                    str(campaign_id),
                    str(subject),
                    str(sent_date).split("T")[0],
                    clean_int(opens),
                    open_rate,
                    clean_int(clicks),
                    click_rate,
                    clean_int(unsubscribes),
                    datetime.now().isoformat(),
                ]

                rows_to_insert.append(row)

            meta = payload.get("meta", {})
            current_page = meta.get("current_page", page)
            last_page = meta.get("last_page", page)

            if current_page >= last_page:
                break

            page += 1

        clear_mailerlite_analytics_sheet()
        append_mailerlite_rows(rows_to_insert)

        append_log(
            "mailerlite_analytics",
            "success",
            len(rows_to_insert),
            "",
        )

        print(f"MailerLite analytics rows inserted: {len(rows_to_insert)}")

    except Exception as e:
        append_log("mailerlite_analytics", "failed", 0, str(e))
        print("MailerLite collector failed:", e)