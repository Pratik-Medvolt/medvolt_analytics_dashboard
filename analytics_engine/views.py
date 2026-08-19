import json
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import (
    Avg,
    Count,
    ExpressionWrapper,
    F,
    FloatField,
    Max,
    Sum,
)
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone


def healthz(request):
    """Unauthenticated liveness endpoint for Docker/monitoring health checks."""
    return HttpResponse("ok", content_type="text/plain")

from analytics_engine.models import (
    MailerLiteAnalytics,
    SearchConsoleAnalytics,
    SystemLog,
    WebsiteAnalytics,
    WeeklyReport,
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
    end_date = timezone.localdate()

    start_date = (
        end_date
        - timedelta(days=days - 1)
    )

    search_data = (
        SearchConsoleAnalytics.objects
        .filter(
            metric_date__gte=start_date,
            metric_date__lte=end_date,
        )
    )

    weighted_position_expression = (
        ExpressionWrapper(
            F("average_position")
            * F("impressions"),
            output_field=FloatField(),
        )
    )

    totals = search_data.aggregate(
        clicks_total=Sum("clicks"),
        impressions_total=Sum(
            "impressions"
        ),
        position_weight_total=Sum(
            weighted_position_expression
        ),
        latest_date=Max("metric_date"),
    )

    clicks = int(
        totals.get("clicks_total") or 0
    )

    impressions = int(
        totals.get(
            "impressions_total"
        )
        or 0
    )

    position_weight = float(
        totals.get(
            "position_weight_total"
        )
        or 0
    )

    ctr = (
        clicks / impressions * 100
        if impressions
        else 0
    )

    average_position = (
        position_weight / impressions
        if impressions
        else 0
    )

    query_rows = list(
        search_data
        .exclude(query="")
        .values("query")
        .annotate(
            clicks_total=Sum("clicks"),
            impressions_total=Sum(
                "impressions"
            ),
        )
        .order_by(
            "-clicks_total",
            "-impressions_total",
        )[:5]
    )

    top_queries = []

    for row in query_rows:
        query_clicks = int(
            row.get("clicks_total") or 0
        )

        query_impressions = int(
            row.get(
                "impressions_total"
            )
            or 0
        )

        query_ctr = (
            query_clicks
            / query_impressions
            * 100
            if query_impressions
            else 0
        )

        top_queries.append(
            {
                "query": row["query"],
                "clicks": query_clicks,
                "impressions": (
                    query_impressions
                ),
                "ctr": round(
                    query_ctr,
                    2,
                ),
            }
        )

    return {
        "overview_search_clicks": clicks,
        "overview_search_impressions": (
            impressions
        ),
        "overview_search_ctr": round(
            ctr,
            2,
        ),
        "overview_search_position": round(
            average_position,
            2,
        ),
        "overview_search_latest_date": (
            totals.get("latest_date")
        ),
        "overview_top_queries": (
            top_queries
        ),
        "overview_search_start_date": (
            start_date
        ),
        "overview_search_end_date": (
            end_date
        ),
    }



@login_required
def overview(request):
    latest_snapshot, mailerlite_campaigns = (
        get_latest_mailerlite_campaigns()
    )

    website_totals = WebsiteAnalytics.objects.aggregate(
        views=Sum("views"),
        sessions=Sum("sessions"),
        users=Sum("users"),
    )

    search_totals = SearchConsoleAnalytics.objects.aggregate(
        clicks=Sum("clicks"),
        impressions=Sum("impressions"),
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

        "website_views": website_totals["views"] or 0,
        "website_sessions": website_totals["sessions"] or 0,
        "website_users": website_totals["users"] or 0,

        "search_clicks": search_totals["clicks"] or 0,
        "search_impressions": (
            search_totals["impressions"] or 0
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


@login_required
def website_dashboard(request):
    """
    GA4 website analytics dashboard.

    Displays website performance for the selected
    7, 30 or 90-day period.
    """

    allowed_periods = {
        "7": 7,
        "30": 30,
        "90": 90,
    }

    selected_period = request.GET.get(
        "days",
        "30",
    )

    days = allowed_periods.get(
        selected_period,
        30,
    )

    latest_metric_date = (
        WebsiteAnalytics.objects.aggregate(
            latest_date=Max("metric_date")
        )["latest_date"]
    )

    if latest_metric_date:
        start_date = (
            latest_metric_date
            - timedelta(days=days - 1)
        )

        website_rows = WebsiteAnalytics.objects.filter(
            metric_date__gte=start_date,
            metric_date__lte=latest_metric_date,
        )

    else:
        start_date = None
        website_rows = WebsiteAnalytics.objects.none()

    totals = website_rows.aggregate(
        total_views=Sum("views"),
        total_sessions=Sum("sessions"),
        total_users=Sum("users"),
        total_engagement=Sum(
            "engagement_time_seconds"
        ),
    )

    total_views = totals["total_views"] or 0
    total_sessions = totals["total_sessions"] or 0
    total_users = totals["total_users"] or 0
    total_engagement = (
        totals["total_engagement"] or 0
    )

    if total_sessions:
        average_engagement = round(
            total_engagement / total_sessions,
            2,
        )
    else:
        average_engagement = 0

    daily_rows = list(
        website_rows.values(
            "metric_date"
        )
        .annotate(
            views=Sum("views"),
            sessions=Sum("sessions"),
            users=Sum("users"),
        )
        .order_by("metric_date")
    )

    daily_labels = [
        row["metric_date"].strftime("%d %b")
        for row in daily_rows
    ]

    daily_views = [
        row["views"] or 0
        for row in daily_rows
    ]

    daily_sessions = [
        row["sessions"] or 0
        for row in daily_rows
    ]

    daily_users = [
        row["users"] or 0
        for row in daily_rows
    ]

    source_rows = list(
        website_rows.values(
            "traffic_source"
        )
        .annotate(
            views=Sum("views"),
            sessions=Sum("sessions"),
            users=Sum("users"),
        )
        .order_by("-views")
    )

    source_labels = [
        row["traffic_source"] or "(not set)"
        for row in source_rows[:10]
    ]

    source_views = [
        row["views"] or 0
        for row in source_rows[:10]
    ]

    top_source = (
        source_rows[0]
        if source_rows
        else None
    )

    top_pages = list(
        website_rows.values(
            "page_url",
            "page_title",
        )
        .annotate(
            views=Sum("views"),
            sessions=Sum("sessions"),
            users=Sum("users"),
            engagement=Sum(
                "engagement_time_seconds"
            ),
        )
        .order_by("-views")[:10]
    )

    for page in top_pages:
        page_sessions = page["sessions"] or 0
        page_engagement = page["engagement"] or 0

        if page_sessions:
            page["average_engagement"] = round(
                page_engagement / page_sessions,
                2,
            )
        else:
            page["average_engagement"] = 0

    top_page_labels = [
        (
            page["page_title"][:35]
            if page["page_title"]
            else page["page_url"][:35]
        )
        for page in top_pages
    ]

    top_page_views = [
        page["views"] or 0
        for page in top_pages
    ]

    page_table = list(
        website_rows.values(
            "page_url",
            "page_title",
        )
        .annotate(
            views=Sum("views"),
            sessions=Sum("sessions"),
            users=Sum("users"),
            engagement=Sum(
                "engagement_time_seconds"
            ),
        )
        .order_by("-views")
    )

    for page in page_table:
        page_sessions = page["sessions"] or 0
        page_engagement = page["engagement"] or 0

        if page_sessions:
            page["average_engagement"] = round(
                page_engagement / page_sessions,
                2,
            )
        else:
            page["average_engagement"] = 0

    latest_log = (
        SystemLog.objects
        .filter(module="ga4_collector")
        .order_by("-started_at")
        .first()
    )

    context = {
        "page_title": "Website Analytics",

        "selected_period": str(days),
        "start_date": start_date,
        "end_date": latest_metric_date,

        "total_views": total_views,
        "total_sessions": total_sessions,
        "total_users": total_users,
        "average_engagement": average_engagement,

        "top_source": top_source,
        "top_pages": top_pages,
        "page_table": page_table,
        "latest_log": latest_log,

        "daily_labels": json.dumps(
            daily_labels
        ),
        "daily_views": json.dumps(
            daily_views
        ),
        "daily_sessions": json.dumps(
            daily_sessions
        ),
        "daily_users": json.dumps(
            daily_users
        ),

        "source_labels": json.dumps(
            source_labels
        ),
        "source_views": json.dumps(
            source_views
        ),

        "top_page_labels": json.dumps(
            top_page_labels
        ),
        "top_page_views": json.dumps(
            top_page_views
        ),
    }

    return render(
        request,
        "dashboard/website.html",
        context,
    )


def calculate_search_metrics(row):
    """
    Calculate CTR and weighted average position
    from aggregated Search Console values.
    """

    clicks = int(
        row.get(
            "total_clicks",
            row.get("clicks", 0),
        )
        or 0
    )

    impressions = int(
        row.get(
            "total_impressions",
            row.get("impressions", 0),
        )
        or 0
    )

    weighted_position = float(
        row.get(
            "weighted_position_sum",
            row.get("weighted_position", 0),
        )
        or 0
    )

    ctr = (
        clicks / impressions * 100
        if impressions
        else 0
    )

    average_position = (
        weighted_position / impressions
        if impressions
        else 0
    )

    return {
        **row,
        "clicks": clicks,
        "impressions": impressions,
        "ctr_percentage": round(ctr, 2),
        "calculated_position": round(
            average_position,
            2,
        ),
    }



@login_required
def search_console(request):
    allowed_periods = {
        7,
        30,
        90,
    }

    try:
        selected_days = int(
            request.GET.get(
                "days",
                30,
            )
        )
    except (TypeError, ValueError):
        selected_days = 30

    if selected_days not in allowed_periods:
        selected_days = 30

    end_date = timezone.localdate()

    start_date = (
        end_date
        - timedelta(
            days=selected_days - 1
        )
    )

    search_data = (
        SearchConsoleAnalytics.objects
        .filter(
            metric_date__gte=start_date,
            metric_date__lte=end_date,
        )
        .select_related("content")
    )

    weighted_position_expression = (
        ExpressionWrapper(
            F("average_position")
            * F("impressions"),
            output_field=FloatField(),
        )
    )

    totals = search_data.aggregate(
        total_clicks=Sum("clicks"),
        total_impressions=Sum("impressions"),
        weighted_position_sum=Sum(
            weighted_position_expression
        ),
        latest_metric_date=Max(
            "metric_date"
        ),
    )

    total_metrics = calculate_search_metrics(
        totals
    )

    daily_rows = list(
        search_data
        .values("metric_date")
        .annotate(
            total_clicks=Sum("clicks"),
            total_impressions=Sum(
                "impressions"
            ),
            weighted_position_sum=Sum(
                weighted_position_expression
        )   ,
        )
        .order_by("metric_date")
    )

    daily_metrics = [
        calculate_search_metrics(row)
        for row in daily_rows
    ]

    query_rows = list(
        search_data
        .exclude(query="")
        .values("query")
        .annotate(
            total_clicks=Sum("clicks"),
            total_impressions=Sum(
                "impressions"
            ),
            weighted_position_sum=Sum(
                weighted_position_expression
            ),
            page_count=Count(
                "page_url",
                distinct=True,
            ),
        )
        .order_by(
            "-total_clicks",
            "-total_impressions",
        )[:20]
    )

    top_queries = [
        calculate_search_metrics(row)
        for row in query_rows
    ]

    page_rows = list(
        search_data
        .values(
            "page_url",
            "content__title",
        )
        .annotate(
            total_clicks=Sum("clicks"),
            total_impressions=Sum(
                "impressions"
            ),
            weighted_position_sum=Sum(
                weighted_position_expression
            ),
            query_count=Count(
                "query",
                distinct=True,
            ),
        )
        .order_by(
            "-total_clicks",
            "-total_impressions",
        )[:20]
    )

    top_pages = [
        calculate_search_metrics(row)
        for row in page_rows
    ]

    latest_log = (
        SystemLog.objects
        .filter(
            module="search_console_collector"
        )
        .order_by("-id")
        .first()
    )

    context = {
        "page_title": "Search Console Analytics",

        "selected_days": selected_days,
        "start_date": start_date,
        "end_date": end_date,

        "total_clicks": total_metrics[
            "clicks"
        ],
        "total_impressions": total_metrics[
            "impressions"
        ],
        "average_ctr": total_metrics[
            "ctr_percentage"
        ],
        "average_position": total_metrics[
            "calculated_position"
        ],
        "latest_metric_date": totals.get(
            "latest_metric_date"
        ),

        "top_queries": top_queries,
        "top_pages": top_pages,
        "latest_log": latest_log,

        "daily_labels": [
            row["metric_date"].strftime(
                "%d %b"
            )
            for row in daily_metrics
        ],
        "daily_clicks": [
            row["clicks"]
            for row in daily_metrics
        ],
        "daily_impressions": [
            row["impressions"]
            for row in daily_metrics
        ],
        "daily_ctr": [
            row["ctr_percentage"]
            for row in daily_metrics
        ],

        "query_labels": [
            row["query"]
            for row in top_queries[:10]
        ],
        "query_clicks": [
            row["clicks"]
            for row in top_queries[:10]
        ],
    }

    return render(
        request,
        "dashboard/search_console.html",
        context,
    )


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