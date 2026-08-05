# from datetime import datetime, timedelta

# from analytics_engine.sheets.sheets_client import (
#     read_content_library,
#     read_ga4_analytics,
#     read_mailerlite_analytics,
#     clear_weekly_report_sheet,
#     append_weekly_report_rows,
#     append_log,
# )


# def safe_float(value):
#     try:
#         return float(value or 0)
#     except Exception:
#         return 0.0


# def normalize(value, max_value):
#     value = safe_float(value)
#     max_value = safe_float(max_value)

#     if max_value <= 0:
#         return 0

#     return round((value / max_value) * 100, 2)


# def get_content_map():
#     records = read_content_library()

#     content_map = {}

#     for row in records:
#         content_id = str(row.get("content_id", "")).strip()

#         if not content_id:
#             continue

#         content_map[content_id] = {
#             "title": row.get("title", ""),
#             "type": row.get("type", ""),
#         }

#     return content_map


# def aggregate_ga4():
#     records = read_ga4_analytics()

#     data = {}

#     for row in records:
#         content_id = str(row.get("content_id", "")).strip()

#         if not content_id:
#             continue

#         if content_id not in data:
#             data[content_id] = {
#                 "views": 0,
#                 "sessions": 0,
#                 "users": 0,
#             }

#         data[content_id]["views"] += safe_float(row.get("views"))
#         data[content_id]["sessions"] += safe_float(row.get("sessions"))
#         data[content_id]["users"] += safe_float(row.get("users"))

#     return data


# def aggregate_mailerlite():
#     records = read_mailerlite_analytics()

#     data = {}

#     for row in records:
#         content_id = str(row.get("content_id", "")).strip()

#         if not content_id:
#             continue

#         data[content_id] = {
#             "open_rate": safe_float(row.get("open_rate")),
#             "click_rate": safe_float(row.get("click_rate")),
#         }

#     return data


# def get_label(score):
#     if score >= 80:
#         return "Top Performer"

#     if score >= 50:
#         return "Average"

#     return "Underperforming"


# def get_recommendation(content_type, score):
#     if score >= 80:
#         return "Repeat this topic and repurpose it into more formats."

#     if score >= 50:
#         return "Keep testing this topic with improved headline and distribution."

#     if content_type == "newsletter":
#         return "Review subject line, audience fit, and CTA placement."

#     return "Avoid repeating this angle unless the topic is strategically important."


# def run_scoring_engine():
#     content_map = get_content_map()
#     ga4_data = aggregate_ga4()
#     mailerlite_data = aggregate_mailerlite()

#     all_content_ids = set()
#     all_content_ids.update(ga4_data.keys())
#     all_content_ids.update(mailerlite_data.keys())

#     max_views = max(
#         [item["views"] for item in ga4_data.values()],
#         default=0,
#     )

#     rows_to_insert = []

#     end_date = datetime.today().date()
#     start_date = end_date - timedelta(days=7)

#     for content_id in all_content_ids:
#         info = content_map.get(content_id, {})
#         content_type = info.get("type", "")

#         ga4 = ga4_data.get(content_id, {})
#         mail = mailerlite_data.get(content_id, {})

#         views = ga4.get("views", 0)
#         sessions = ga4.get("sessions", 0)
#         users = ga4.get("users", 0)

#         open_rate = mail.get("open_rate", 0)
#         click_rate = mail.get("click_rate", 0)

#         ga4_score = normalize(views, max_views)

#         newsletter_score = min(
#             round((open_rate * 0.6) + (click_rate * 0.4), 2),
#             100,
#         )

#         if content_type == "newsletter":
#             performance_score = newsletter_score
#         elif content_type in ["blog", "website_page", "whitepaper", "event", "case_study"]:
#             performance_score = ga4_score
#         else:
#             performance_score = max(ga4_score, newsletter_score)

#         label = get_label(performance_score)
#         recommendation = get_recommendation(content_type, performance_score)

#         rows_to_insert.append([
#             str(start_date),
#             str(end_date),
#             content_id,
#             info.get("title", ""),
#             content_type,
#             views,
#             sessions,
#             users,
#             open_rate,
#             click_rate,
#             performance_score,
#             label,
#             recommendation,
#             datetime.now().isoformat(),
#         ])

#     rows_to_insert.sort(key=lambda x: safe_float(x[10]), reverse=True)

#     clear_weekly_report_sheet()
#     append_weekly_report_rows(rows_to_insert)

#     append_log(
#         "scoring_engine",
#         "success",
#         len(rows_to_insert),
#         "",
#     )

#     print(f"Scoring rows inserted: {len(rows_to_insert)}")




from datetime import datetime, timedelta

from analytics_engine.sheets.sheets_client import (
    read_content_library,
    read_ga4_analytics,
    read_mailerlite_analytics,
    clear_weekly_report_sheet,
    append_weekly_report_rows,
    append_log,
)


def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def normalize(value, max_value):
    value = safe_float(value)
    max_value = safe_float(max_value)

    if max_value <= 0:
        return 0

    return round((value / max_value) * 100, 2)


def get_content_map():
    records = read_content_library()
    content_map = {}

    for row in records:
        content_id = str(row.get("content_id", "")).strip()

        if not content_id:
            continue

        content_map[content_id] = {
            "title": row.get("title", ""),
            "type": str(row.get("type", "")).lower(),
            "platform": str(row.get("platform", "")).lower(),
            "website_url": row.get("website_url", ""),
            "substack_url": row.get("substack_url", ""),
            "canonical_url": row.get("canonical_url", ""),
        }

    return content_map


def aggregate_ga4():
    records = read_ga4_analytics()
    data = {}

    for row in records:
        content_id = str(row.get("content_id", "")).strip()

        if not content_id:
            continue

        if content_id not in data:
            data[content_id] = {
                "views": 0,
                "sessions": 0,
                "users": 0,
            }

        data[content_id]["views"] += safe_float(row.get("views"))
        data[content_id]["sessions"] += safe_float(row.get("sessions"))
        data[content_id]["users"] += safe_float(row.get("users"))

    return data


def aggregate_mailerlite():
    records = read_mailerlite_analytics()
    data = {}

    for row in records:
        content_id = str(row.get("content_id", "")).strip()

        if not content_id:
            continue

        data[content_id] = {
            "open_rate": safe_float(row.get("open_rate")),
            "click_rate": safe_float(row.get("click_rate")),
        }

    return data


def calculate_newsletter_score(open_rate, click_rate):
    open_rate = safe_float(open_rate)
    click_rate = safe_float(click_rate)

    score = 0

    if open_rate >= 60:
        score += 40
    elif open_rate >= 40:
        score += 25
    elif open_rate >= 20:
        score += 10

    if click_rate >= 10:
        score += 60
    elif click_rate >= 5:
        score += 35
    elif click_rate >= 2:
        score += 15

    return min(score, 100)


def get_label(score):
    score = safe_float(score)

    if score >= 80:
        return "Top Performer"

    if score >= 50:
        return "Average"

    if score > 0:
        return "Underperforming"

    return "No Data Yet"


def get_recommendation(content_type, score, has_data):
    if not has_data:
        return "No analytics data yet. Keep collecting before making a decision."

    if score >= 80:
        return "Repeat this topic and repurpose it into more formats."

    if score >= 50:
        return "Keep testing this topic with improved headline and distribution."

    if content_type in ["newsletter", "blog"]:
        return "Review subject line, hook, audience fit, and CTA placement."

    return "Avoid repeating this angle unless the topic is strategically important."


def run_scoring_engine():
    content_map = get_content_map()
    ga4_data = aggregate_ga4()
    mailerlite_data = aggregate_mailerlite()

    max_views = max(
        [item["views"] for item in ga4_data.values()],
        default=0,
    )

    rows_to_insert = []

    end_date = datetime.today().date()
    start_date = end_date - timedelta(days=7)

    for content_id, info in content_map.items():
        content_type = info.get("type", "")

        ga4 = ga4_data.get(content_id, {})
        mail = mailerlite_data.get(content_id, {})

        views = ga4.get("views", 0)
        sessions = ga4.get("sessions", 0)
        users = ga4.get("users", 0)

        open_rate = mail.get("open_rate", 0)
        click_rate = mail.get("click_rate", 0)

        ga4_score = normalize(views, max_views)
        newsletter_score = calculate_newsletter_score(open_rate, click_rate)

        has_ga4_data = content_id in ga4_data
        has_mailerlite_data = content_id in mailerlite_data
        has_data = has_ga4_data or has_mailerlite_data

        if content_type == "newsletter":
            performance_score = newsletter_score

        elif content_type == "blog":
            if has_ga4_data and has_mailerlite_data:
                performance_score = round((ga4_score * 0.5) + (newsletter_score * 0.5), 2)
            elif has_mailerlite_data:
                performance_score = newsletter_score
            elif has_ga4_data:
                performance_score = ga4_score
            else:
                performance_score = 0

        elif content_type in ["website_page", "whitepaper", "event", "case_study", "product_page"]:
            performance_score = ga4_score

        else:
            performance_score = max(ga4_score, newsletter_score)

        label = get_label(performance_score)
        recommendation = get_recommendation(content_type, performance_score, has_data)

        rows_to_insert.append([
            str(start_date),
            str(end_date),
            content_id,
            info.get("title", ""),
            content_type,
            views,
            sessions,
            users,
            open_rate,
            click_rate,
            performance_score,
            label,
            recommendation,
            datetime.now().isoformat(),
        ])

    rows_to_insert.sort(key=lambda x: safe_float(x[10]), reverse=True)

    clear_weekly_report_sheet()
    append_weekly_report_rows(rows_to_insert)

    append_log(
        "scoring_engine",
        "success",
        len(rows_to_insert),
        "",
    )

    print(f"Scoring rows inserted: {len(rows_to_insert)}")