"""
Website Analytics dashboard data.

Every value comes from the GA4 report that defines it for the exact
selected range:

    summary          <- GA4Report "summary"        (no dimensions)
    daily            <- GA4DailyMetric             (chart only)
    traffic_sources  <- GA4Report "source_medium"
    channel_groups   <- GA4Report "channel_group"
    pages            <- GA4Report "page"

Sessions and users are non-additive. Do not "fix" a missing summary by
summing daily, page or source rows - it over-counts.
"""

from analytics_engine.collectors.ga4_reports import GA4_PROPERTY_ID
from analytics_engine.collectors.ga4_db_collector import (
    PRESET_PERIODS,
    build_page_url,
)
from analytics_engine.models import (
    GA4DailyMetric,
    GA4Report,
    SystemLog,
)


def get_preset_range(days):
    """Latest collected (start_date, end_date) for a "Last N days" preset."""

    period_key = f"last_{days}_days"

    if period_key not in PRESET_PERIODS:
        return None

    report = (
        GA4Report.objects.filter(
            property_id=GA4_PROPERTY_ID,
            report_type=GA4Report.REPORT_SUMMARY,
            period_key=period_key,
        )
        .order_by("-end_date")
        .first()
    )

    if not report:
        return None

    return report.start_date, report.end_date


def get_range_reports(start_date, end_date):
    reports = GA4Report.objects.filter(
        property_id=GA4_PROPERTY_ID,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        report.report_type: report
        for report in reports
    }


def has_complete_range(start_date, end_date):
    return set(get_range_reports(start_date, end_date)) >= {
        report_type
        for report_type, _ in GA4Report.REPORT_TYPE_CHOICES
    }


def get_range_summary(start_date, end_date):
    """Site totals row for an exact range, or None if not collected."""

    report = get_range_reports(start_date, end_date).get(
        GA4Report.REPORT_SUMMARY
    )

    if not report:
        return None

    return report.rows.first()


def serialize_summary(row):
    if row is None:
        return None

    return {
        "views": row.views,
        "sessions": row.sessions,
        "total_users": row.total_users,
        "active_users": row.active_users,
        "new_users": row.new_users,
        "engaged_sessions": row.engaged_sessions,
        "event_count": row.event_count,
        "engagement_duration_seconds": row.engagement_duration_seconds,
        "engagement_rate": row.engagement_rate,
        "avg_engagement_time_per_session": (
            row.avg_engagement_time_per_session
        ),
        "avg_engagement_time_per_active_user": (
            row.avg_engagement_time_per_active_user
        ),
    }


def serialize_acquisition_rows(report, label_key):
    if not report:
        return []

    rows = list(
        report.rows.order_by("-sessions", "dimension_value")
    )

    total_sessions = sum(row.sessions or 0 for row in rows)

    return [
        {
            label_key: row.dimension_value,
            "sessions": row.sessions,
            "engaged_sessions": row.engaged_sessions,
            "engagement_rate": row.engagement_rate,
            "avg_engagement_time_per_session": (
                row.avg_engagement_time_per_session
            ),
            # Sessions are additive across session-scoped acquisition
            # dimensions (each session has exactly one source / medium).
            "session_share": (
                (row.sessions or 0) / total_sessions
                if total_sessions
                else None
            ),
        }
        for row in rows
    ]


def serialize_pages(report):
    if not report:
        return []

    rows = report.rows.order_by("-views", "dimension_value")

    return [
        {
            "page_path": row.dimension_value,
            "page_title": row.page_title,
            "page_url": build_page_url(row.dimension_value),
            "views": row.views,
            "active_users": row.active_users,
            "event_count": row.event_count,
            "avg_engagement_time_per_active_user": (
                row.avg_engagement_time_per_active_user
            ),
        }
        for row in rows
    ]


def serialize_daily(start_date, end_date):
    rows = GA4DailyMetric.objects.filter(
        property_id=GA4_PROPERTY_ID,
        metric_date__range=(start_date, end_date),
    ).order_by("metric_date")

    return [
        {
            "date": row.metric_date,
            "views": row.views,
            "sessions": row.sessions,
            "total_users": row.total_users,
            "active_users": row.active_users,
        }
        for row in rows
    ]


def serialize_collector():
    log = (
        SystemLog.objects.filter(module="ga4_collector")
        .order_by("-started_at")
        .first()
    )

    if not log:
        return None

    return {
        "status": log.status,
        "started_at": log.started_at,
        "completed_at": log.completed_at,
        # Database rows written by the run - not a traffic metric.
        "rows_written": log.records_processed,
        "error_message": log.error_message,
    }


def build_website_dashboard_payload(start_date, end_date):
    reports = get_range_reports(start_date, end_date)

    summary_report = reports.get(GA4Report.REPORT_SUMMARY)

    missing_reports = [
        report_type
        for report_type, _ in GA4Report.REPORT_TYPE_CHOICES
        if report_type not in reports
    ]

    synced_times = [
        report.collected_at
        for report in reports.values()
    ]

    return {
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            "days": (end_date - start_date).days + 1,
            "property_id": GA4_PROPERTY_ID,
            "property_timezone": (
                summary_report.property_timezone
                if summary_report
                else ""
            ),
            # Oldest report sync, i.e. how fresh the whole view is.
            "last_synced": min(synced_times) if synced_times else None,
            "missing_reports": missing_reports,
        },
        "summary": serialize_summary(
            summary_report.rows.first()
            if summary_report
            else None
        ),
        "daily": serialize_daily(start_date, end_date),
        "traffic_sources": serialize_acquisition_rows(
            reports.get(GA4Report.REPORT_SOURCE_MEDIUM),
            "source_medium",
        ),
        "channel_groups": serialize_acquisition_rows(
            reports.get(GA4Report.REPORT_CHANNEL_GROUP),
            "channel_group",
        ),
        "pages": serialize_pages(
            reports.get(GA4Report.REPORT_PAGE)
        ),
        "collector": serialize_collector(),
    }
