import json
from datetime import date, datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import (
    Avg,
    Max,
    Sum,
)
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone


def healthz(request):
    """Unauthenticated liveness endpoint for Docker/monitoring health checks."""
    return HttpResponse("ok", content_type="text/plain")

from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_range,
)
from analytics_engine.collectors.search_console_db_collector import (
    collect_search_console_range,
)
from analytics_engine.collectors.search_console_reports import (
    GSC_RETENTION_DAYS,
    dashboard_today,
    preset_range,
)
from analytics_engine.models import (
    MailerLiteAnalytics,
    SystemLog,
    WeeklyReport,
)
from analytics_engine.services import search_console_dashboard
from analytics_engine.services.website_dashboard import (
    build_website_dashboard_payload,
    get_preset_range,
    get_range_summary,
    has_complete_range,
    serialize_collector,
)


def get_latest_mailerlite_campaigns():
    latest_snapshot = (
        MailerLiteAnalytics.objects.aggregate(
            latest=Max("snapshot_date")
        )["latest"]
    )

    if not latest_snapshot:
        return None, MailerLiteAnalytics.objects.none()

    campaigns = MailerLiteAnalytics.objects.filter(
        snapshot_date=latest_snapshot
    )

    return latest_snapshot, campaigns


def get_search_console_overview_data(days=30):
    """
    Overview search block: the "N Days" preset ending today. KPIs from
    the property summary, queries from the query report - never summed
    rows. If today's range cannot be fetched, the latest collected
    preset is shown with its own dates.
    """

    search_range = preset_range(days, dashboard_today())

    if ensure_search_console_range(*search_range, days=days):
        search_range = search_console_dashboard.get_preset_range(days)

    payload = (
        search_console_dashboard.build_search_console_payload(
            *search_range
        )
        if search_range
        else None
    )

    summary = (payload["summary"] if payload else None) or {}
    period = payload["period"] if payload else {}

    return {
        "overview_search_clicks": summary.get("clicks", 0),
        "overview_search_impressions": summary.get("impressions", 0),
        "overview_search_ctr": summary.get("ctr_percent"),
        "overview_search_position": summary.get("position"),
        "overview_search_latest_date": period.get("available_through"),
        "overview_search_preliminary_start": period.get(
            "preliminary_start"
        ),
        "overview_search_pending_start": period.get("pending_start"),
        "overview_top_queries": (
            payload["queries"][:5] if payload else []
        ),
        "overview_search_start_date": period.get("start_date"),
        "overview_search_end_date": period.get("end_date"),
    }


@login_required
def overview(request):
    latest_snapshot, mailerlite_campaigns = (
        get_latest_mailerlite_campaigns()
    )

    # Site totals from the GA4 summary report - never summed page rows.
    website_range = get_preset_range(30)

    website_summary = (
        get_range_summary(*website_range)
        if website_range
        else None
    )

    mailerlite_totals = mailerlite_campaigns.aggregate(
        opens=Sum("opens"),
        clicks=Sum("clicks"),
        open_rate=Avg("open_rate"),
        click_rate=Avg("click_rate"),
    )

    recent_logs = SystemLog.objects.order_by(
        "-started_at"
    )[:5]

    context = {
        "page_title": "Overview",

        "website_views": (
            website_summary.views if website_summary else 0
        ),
        "website_sessions": (
            website_summary.sessions if website_summary else 0
        ),
        "website_total_users": (
            website_summary.total_users if website_summary else 0
        ),
        "website_start_date": (
            website_range[0] if website_range else None
        ),
        "website_end_date": (
            website_range[1] if website_range else None
        ),

        "newsletter_opens": (
            mailerlite_totals["opens"] or 0
        ),
        "newsletter_clicks": (
            mailerlite_totals["clicks"] or 0
        ),
        "average_open_rate": round(
            mailerlite_totals["open_rate"] or 0,
            2,
        ),
        "average_click_rate": round(
            mailerlite_totals["click_rate"] or 0,
            2,
        ),

        "latest_snapshot": latest_snapshot,
        "recent_logs": recent_logs,
    }

    context.update(
        get_search_console_overview_data(
            days=30
        )
    )

    # Same 30-day property totals as the Search Console block.
    context["search_clicks"] = context["overview_search_clicks"]

    return render(
        request,
        "dashboard/overview.html",
        context,
    )


@login_required
def mailerlite_dashboard(request):
    latest_snapshot, campaigns = (
        get_latest_mailerlite_campaigns()
    )

    totals = campaigns.aggregate(
        opens=Sum("opens"),
        clicks=Sum("clicks"),
        unsubscribes=Sum("unsubscribes"),
        open_rate=Avg("open_rate"),
        click_rate=Avg("click_rate"),
    )

    campaign_table = campaigns.order_by(
        "-sent_date",
        "-opens",
    )

    top_campaigns = campaigns.order_by(
        "-opens"
    )[:8]

    chart_labels = [
        campaign.subject[:35]
        for campaign in top_campaigns
    ]

    chart_opens = [
        campaign.opens
        for campaign in top_campaigns
    ]

    chart_clicks = [
        campaign.clicks
        for campaign in top_campaigns
    ]

    latest_log = (
        SystemLog.objects
        .filter(module="mailerlite_collector")
        .order_by("-started_at")
        .first()
    )

    context = {
        "page_title": "MailerLite Analytics",

        "latest_snapshot": latest_snapshot,
        "campaign_count": campaigns.count(),

        "total_opens": totals["opens"] or 0,
        "total_clicks": totals["clicks"] or 0,
        "total_unsubscribes": (
            totals["unsubscribes"] or 0
        ),

        "average_open_rate": round(
            totals["open_rate"] or 0,
            2,
        ),
        "average_click_rate": round(
            totals["click_rate"] or 0,
            2,
        ),

        "campaigns": campaign_table,
        "latest_log": latest_log,

        "chart_labels": json.dumps(
            chart_labels
        ),
        "chart_opens": json.dumps(
            chart_opens
        ),
        "chart_clicks": json.dumps(
            chart_clicks
        ),
    }

    return render(
        request,
        "dashboard/mailerlite.html",
        context,
    )


WEBSITE_PERIODS = {
    "7": 7,
    "30": 30,
    "90": 90,
}

# Earliest date the GA4 Data API accepts, and the longest custom range.
GA4_MIN_DATE = date(2015, 8, 14)
WEBSITE_MAX_CUSTOM_DAYS = 400


def parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def resolve_website_period(request):
    """
    Resolve the selected period to an inclusive (start_date, end_date).

    ?start=YYYY-MM-DD&end=YYYY-MM-DD selects an exact custom range;
    otherwise ?days=7|30|90 selects the latest collected GA4
    "Last N days" preset.

    Returns (start_date, end_date, selected_period, error).
    """

    start_value = request.GET.get("start")
    end_value = request.GET.get("end")

    if start_value or end_value:
        start_date = parse_iso_date(start_value)
        end_date = parse_iso_date(end_value)

        if not start_date or not end_date:
            return None, None, "custom", (
                "Enter both dates as YYYY-MM-DD."
            )

        if start_date > end_date:
            return None, None, "custom", (
                "The start date must be on or before the end date."
            )

        if start_date < GA4_MIN_DATE:
            return None, None, "custom", (
                f"GA4 data is not available before {GA4_MIN_DATE}."
            )

        if (end_date - start_date).days + 1 > WEBSITE_MAX_CUSTOM_DAYS:
            return None, None, "custom", (
                f"Custom ranges are limited to "
                f"{WEBSITE_MAX_CUSTOM_DAYS} days."
            )

        return start_date, end_date, "custom", None

    selected_period = request.GET.get("days", "30")

    if selected_period not in WEBSITE_PERIODS:
        selected_period = "30"

    preset = get_preset_range(WEBSITE_PERIODS[selected_period])

    if not preset:
        return None, None, selected_period, None

    return preset[0], preset[1], selected_period, None


def ensure_website_range(start_date, end_date):
    """
    Custom ranges not collected yet are fetched from GA4 on demand and
    stored like any other range. Returns an error message or None.
    """

    if has_complete_range(start_date, end_date):
        return None

    try:
        collect_ga4_range(start_date, end_date)
    except Exception as error:
        return f"Could not fetch this range from GA4: {error}"

    return None


@login_required
def website_dashboard(request):
    """
    GA4 website analytics dashboard.

    KPI cards, daily chart, acquisition and pages each come from their own
    GA4 report for the exact selected range - see
    services/website_dashboard.py.
    """

    start_date, end_date, selected_period, error = (
        resolve_website_period(request)
    )

    if start_date and selected_period == "custom":
        error = ensure_website_range(start_date, end_date)

    payload = None

    if start_date and not error:
        payload = build_website_dashboard_payload(
            start_date,
            end_date,
        )

    summary = payload["summary"] if payload else None
    daily = payload["daily"] if payload else []
    traffic_sources = payload["traffic_sources"] if payload else []
    pages = payload["pages"] if payload else []

    top_pages = pages[:10]

    context = {
        "page_title": "Website Analytics",

        "selected_period": selected_period,
        "start_date": start_date,
        "end_date": end_date,
        "period_error": error,

        "period": payload["period"] if payload else None,
        "summary": summary,
        "traffic_sources": traffic_sources,
        "channel_groups": (
            payload["channel_groups"] if payload else []
        ),
        "pages": pages,
        "collector": serialize_collector(),

        "daily_labels": json.dumps(
            [row["date"].strftime("%d %b") for row in daily]
        ),
        "daily_views": json.dumps(
            [row["views"] or 0 for row in daily]
        ),
        "daily_sessions": json.dumps(
            [row["sessions"] or 0 for row in daily]
        ),
        "daily_total_users": json.dumps(
            [row["total_users"] or 0 for row in daily]
        ),

        "source_labels": json.dumps(
            [row["source_medium"] for row in traffic_sources]
        ),
        "source_sessions": json.dumps(
            [row["sessions"] or 0 for row in traffic_sources]
        ),

        "top_page_labels": json.dumps(
            [
                (page["page_title"] or page["page_path"])[:35]
                for page in top_pages
            ]
        ),
        "top_page_paths": json.dumps(
            [page["page_path"] for page in top_pages]
        ),
        "top_page_views": json.dumps(
            [page["views"] or 0 for page in top_pages]
        ),
    }

    return render(
        request,
        "dashboard/website.html",
        context,
    )


@login_required
def website_dashboard_data(request):
    """JSON form of the Website Analytics dashboard data."""

    start_date, end_date, selected_period, error = (
        resolve_website_period(request)
    )

    if start_date and selected_period == "custom":
        error = ensure_website_range(start_date, end_date)

    if error:
        return JsonResponse({"error": error}, status=400)

    if not start_date:
        return JsonResponse(
            {"error": "No GA4 data has been collected yet."},
            status=404,
        )

    return JsonResponse(
        build_website_dashboard_payload(start_date, end_date),
        encoder=DjangoJSONEncoder,
    )


SEARCH_CONSOLE_PERIODS = {
    "7": 7,
    "30": 30,
    "90": 90,
}


def resolve_search_console_period(request):
    """
    Resolve the selected period to an inclusive (start_date, end_date).

    ?start=YYYY-MM-DD&end=YYYY-MM-DD selects an exact custom range;
    otherwise ?days=7|30|90 selects the last N calendar dates ending
    today (DASHBOARD_TIME_ZONE), e.g. 7 Days on 25 Sep = 19-25 Sep. Dates Google
    has no data for yet are shown as pending; the range is never
    shifted back to Google's latest available date.

    Returns (start_date, end_date, selected_period, error).
    """

    start_value = request.GET.get("start")
    end_value = request.GET.get("end")

    if start_value or end_value:
        start_date = parse_iso_date(start_value)
        end_date = parse_iso_date(end_value)

        if not start_date or not end_date:
            return None, None, "custom", (
                "Enter both dates as YYYY-MM-DD."
            )

        if start_date > end_date:
            return None, None, "custom", (
                "The start date must be on or before the end date."
            )

        today = dashboard_today()

        if end_date > today:
            return None, None, "custom", (
                f"The end date cannot be after today ({today})."
            )

        if start_date < today - timedelta(days=GSC_RETENTION_DAYS):
            return None, None, "custom", (
                "Search Console keeps 16 months of data."
            )

        return start_date, end_date, "custom", None

    selected_period = request.GET.get("days", "30")

    if selected_period not in SEARCH_CONSOLE_PERIODS:
        selected_period = "30"

    start_date, end_date = preset_range(
        SEARCH_CONSOLE_PERIODS[selected_period],
        dashboard_today(),
    )

    return start_date, end_date, selected_period, None


def ensure_search_console_range(start_date, end_date, days=None):
    """
    Ranges not collected yet (a custom range, or today's preset before
    the collector has run today), or missing dates the collector has
    stored since, are fetched on demand and stored like any other range.
    Returns an error message or None.
    """

    if search_console_dashboard.is_range_current(start_date, end_date):
        return None

    try:
        collect_search_console_range(
            start_date,
            end_date,
            period_key=f"last_{days}_days" if days else "custom",
        )
    except Exception as error:
        return (
            "Could not fetch this range from Search Console: "
            f"{error}"
        )

    return None


def get_search_console_request_payload(request):
    start_date, end_date, selected_period, error = (
        resolve_search_console_period(request)
    )

    if start_date and not error:
        error = ensure_search_console_range(
            start_date,
            end_date,
            days=SEARCH_CONSOLE_PERIODS.get(selected_period),
        )

    payload = None

    if start_date and not error:
        payload = search_console_dashboard.build_search_console_payload(
            start_date,
            end_date,
        )

    return start_date, end_date, selected_period, error, payload


@login_required
def search_console(request):
    """
    Search Console dashboard.

    KPI cards, daily chart, queries and pages each come from their own
    Search Analytics report for the exact selected range - see
    services/search_console_dashboard.py.
    """

    start_date, end_date, selected_period, error, payload = (
        get_search_console_request_payload(request)
    )

    daily = payload["daily"] if payload else []
    queries = payload["queries"] if payload else []
    pages = payload["pages"] if payload else []

    top_queries = queries[:10]

    context = {
        "page_title": "Search Console Analytics",

        "selected_period": selected_period,
        "start_date": start_date,
        "end_date": end_date,
        "period_error": error,

        "period": payload["period"] if payload else None,
        "summary": payload["summary"] if payload else None,
        "queries": queries[:25],
        "query_count": len(queries),
        "pages": pages[:25],
        "page_count": len(pages),
        "collector": search_console_dashboard.serialize_collector(),

        # Pending dates are null so the chart shows a gap, not a zero.
        "daily_labels": [row["date"].strftime("%d %b") for row in daily],
        "daily_clicks": [row["clicks"] for row in daily],
        "daily_impressions": [row["impressions"] for row in daily],
        "daily_status": [row["status"] for row in daily],

        "query_labels": [row["query"] for row in top_queries],
        "query_clicks": [row["clicks"] for row in top_queries],
    }

    return render(
        request,
        "dashboard/search_console.html",
        context,
    )


@login_required
def search_console_data(request):
    """JSON form of the Search Console dashboard data."""

    start_date, _, _, error, payload = (
        get_search_console_request_payload(request)
    )

    if error:
        return JsonResponse({"error": error}, status=400)

    if not start_date:
        return JsonResponse(
            {"error": "No Search Console data has been collected yet."},
            status=404,
        )

    return JsonResponse(payload, encoder=DjangoJSONEncoder)


def calculate_report_change(
    current_value,
    previous_value,
):
    if previous_value in (None, 0):
        return None

    return round(
        (
            (
                current_value
                - previous_value
            )
            / previous_value
        )
        * 100,
        1,
    )

@login_required
def weekly_reports(request):
    reports = WeeklyReport.objects.all()

    selected_report = None
    selected_week = request.GET.get("week")

    if selected_week:
        try:
            selected_week_date = datetime.strptime(
                selected_week,
                "%Y-%m-%d",
            ).date()

            selected_report = (
                reports
                .filter(
                    week_start=selected_week_date
                )
                .first()
            )

        except ValueError:
            selected_report = None

    if selected_report is None:
        selected_report = reports.first()

    previous_report = None

    website_views_change = None
    website_sessions_change = None
    search_clicks_change = None
    search_impressions_change = None
    email_opens_change = None
    email_clicks_change = None

    search_ctr = 0
    previous_search_ctr = 0
    search_ctr_change = None

    recommendation_items = []

    if selected_report:
        previous_report = (
            WeeklyReport.objects
            .filter(
                week_start=(
                    selected_report.week_start
                    - timedelta(days=7)
                )
            )
            .first()
        )

        if selected_report.total_search_impressions:
            search_ctr = round(
                (
                    selected_report
                    .total_search_clicks
                    / selected_report
                    .total_search_impressions
                )
                * 100,
                2,
            )

        if previous_report:
            if previous_report.total_search_impressions:
                previous_search_ctr = round(
                    (
                        previous_report
                        .total_search_clicks
                        / previous_report
                        .total_search_impressions
                    )
                    * 100,
                    2,
                )

            website_views_change = (
                calculate_report_change(
                    selected_report
                    .total_website_views,
                    previous_report
                    .total_website_views,
                )
            )

            website_sessions_change = (
                calculate_report_change(
                    selected_report
                    .total_website_sessions,
                    previous_report
                    .total_website_sessions,
                )
            )

            search_clicks_change = (
                calculate_report_change(
                    selected_report
                    .total_search_clicks,
                    previous_report
                    .total_search_clicks,
                )
            )

            search_impressions_change = (
                calculate_report_change(
                    selected_report
                    .total_search_impressions,
                    previous_report
                    .total_search_impressions,
                )
            )

            email_opens_change = (
                calculate_report_change(
                    selected_report
                    .total_email_opens,
                    previous_report
                    .total_email_opens,
                )
            )

            email_clicks_change = (
                calculate_report_change(
                    selected_report
                    .total_email_clicks,
                    previous_report
                    .total_email_clicks,
                )
            )

            search_ctr_change = (
                calculate_report_change(
                    search_ctr,
                    previous_search_ctr,
                )
            )

        recommendation_items = [
            item.strip()
            for item
            in selected_report
            .recommendations
            .split("\n\n")
            if item.strip()
        ]

    report_history = reports[:12]

    latest_log = (
        SystemLog.objects
        .filter(
            module="weekly_report_generator"
        )
        .order_by("-id")
        .first()
    )

    context = {
        "page_title": "Weekly Reports",

        "selected_report": selected_report,
        "previous_report": previous_report,
        "report_history": report_history,

        "website_views_change": (
            website_views_change
        ),
        "website_sessions_change": (
            website_sessions_change
        ),
        "search_clicks_change": (
            search_clicks_change
        ),
        "search_impressions_change": (
            search_impressions_change
        ),
        "email_opens_change": (
            email_opens_change
        ),
        "email_clicks_change": (
            email_clicks_change
        ),

        "search_ctr": search_ctr,
        "search_ctr_change": (
            search_ctr_change
        ),

        "recommendation_items": (
            recommendation_items
        ),
        "latest_log": latest_log,
    }

    return render(
        request,
        "dashboard/weekly_reports.html",
        context,
    )

@login_required
def system_health(request):
    display_names = {
        "content_discovery": "Content Discovery",
        "mailerlite_collector": "MailerLite",
        "ga4_collector": "GA4 Website Analytics",
        "search_console_collector": "Search Console",
        "weekly_report_generator": "Weekly Reports",
    }

    module_order = [
        "content_discovery",
        "mailerlite_collector",
        "ga4_collector",
        "search_console_collector",
        "weekly_report_generator",
    ]

    available_modules = list(
        SystemLog.objects
        .order_by()
        .values_list(
            "module",
            flat=True,
        )
        .distinct()
    )

    latest_logs_by_module = {}

    for module in available_modules:
        latest_log = (
            SystemLog.objects
            .filter(module=module)
            .order_by("-id")
            .first()
        )

        if latest_log:
            latest_log.display_name = (
                display_names.get(
                    module,
                    module.replace(
                        "_",
                        " ",
                    ).title(),
                )
            )

            latest_logs_by_module[
                module
            ] = latest_log

    latest_logs = []

    for module in module_order:
        if module in latest_logs_by_module:
            latest_logs.append(
                latest_logs_by_module.pop(
                    module
                )
            )

    latest_logs.extend(
        latest_logs_by_module.values()
    )

    success_count = sum(
        1
        for log in latest_logs
        if log.status == "success"
    )

    failed_count = sum(
        1
        for log in latest_logs
        if log.status == "failed"
    )

    active_count = sum(
        1
        for log in latest_logs
        if log.status in {
            "running",
            "warning",
        }
    )

    completion_times = [
        log.completed_at
        for log in latest_logs
        if log.completed_at
    ]

    last_activity = (
        max(completion_times)
        if completion_times
        else None
    )

    if failed_count:
        overall_status = "failed"
    elif active_count:
        overall_status = "warning"
    elif latest_logs:
        overall_status = "success"
    else:
        overall_status = "no_data"

    context = {
        "page_title": "System Health",
        "latest_logs": latest_logs,
        "module_count": len(
            latest_logs
        ),
        "success_count": success_count,
        "failed_count": failed_count,
        "active_count": active_count,
        "last_activity": last_activity,
        "overall_status": overall_status,
    }

    return render(
        request,
        "dashboard/system_health.html",
        context,
    )