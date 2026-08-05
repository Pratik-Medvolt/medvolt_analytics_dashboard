from datetime import timedelta

from django.db import models, transaction
from django.db.models import Avg, Sum
from django.utils import timezone

from analytics_engine.models import (
    MailerLiteAnalytics,
    SearchConsoleAnalytics,
    SystemLog,
    WebsiteAnalytics,
    WeeklyReport,
)


def get_previous_completed_week(reference_date=None):
    """
    Return the previous completed Monday-Sunday period.
    """

    current_date = (
        reference_date
        or timezone.localdate()
    )

    current_monday = (
        current_date
        - timedelta(
            days=current_date.weekday()
        )
    )

    week_start = (
        current_monday
        - timedelta(days=7)
    )

    week_end = (
        week_start
        + timedelta(days=6)
    )

    return week_start, week_end


def get_mailerlite_campaign_snapshots(
    week_start,
    week_end,
):
    """
    Select campaigns sent during the selected week.

    For each campaign, use only its latest available
    analytics snapshot up to the end of that week.
    """

    sent_date_field = (
        MailerLiteAnalytics._meta.get_field(
            "sent_date"
        )
    )

    campaign_filters = {
        "sent_date__isnull": False,
    }

    if isinstance(
        sent_date_field,
        models.DateTimeField,
    ):
        campaign_filters[
            "sent_date__date__range"
        ] = (
            week_start,
            week_end,
        )
    else:
        campaign_filters[
            "sent_date__range"
        ] = (
            week_start,
            week_end,
        )

    campaign_ids = (
        MailerLiteAnalytics.objects
        .filter(**campaign_filters)
        .exclude(campaign_id="")
        .order_by()
        .values_list(
            "campaign_id",
            flat=True,
        )
        .distinct()
    )

    campaign_snapshots = []

    for campaign_id in campaign_ids:
        latest_snapshot = (
            MailerLiteAnalytics.objects
            .filter(
                campaign_id=campaign_id,
                snapshot_date__lte=week_end,
            )
            .order_by(
                "-snapshot_date",
                "-id",
            )
            .first()
        )

        if latest_snapshot:
            campaign_snapshots.append(
                latest_snapshot
            )

    return campaign_snapshots


def calculate_percentage_change(
    current_value,
    previous_value,
):
    if not previous_value:
        return None

    return (
        (
            current_value
            - previous_value
        )
        / previous_value
        * 100
    )


def build_recommendations(
    website_views,
    website_sessions,
    search_clicks,
    search_impressions,
    email_campaign_count,
    email_open_rate,
    email_click_rate,
    previous_report=None,
):
    recommendations = []

    if previous_report:
        website_change = (
            calculate_percentage_change(
                website_views,
                previous_report
                .total_website_views,
            )
        )

        if website_change is not None:
            if website_change >= 10:
                recommendations.append(
                    "Website views increased by "
                    f"{website_change:.1f}% compared "
                    "with the previous week. Review "
                    "the top-performing pages and "
                    "traffic sources to identify what "
                    "contributed to the growth."
                )

            elif website_change <= -10:
                recommendations.append(
                    "Website views decreased by "
                    f"{abs(website_change):.1f}% "
                    "compared with the previous week. "
                    "Review acquisition sources, recent "
                    "content activity and any tracking "
                    "changes."
                )

            else:
                recommendations.append(
                    "Website traffic remained relatively "
                    "stable compared with the previous "
                    "week."
                )
    elif website_views == 0:
        recommendations.append(
            "No website views were recorded for this "
            "week. Confirm that GA4 collection completed "
            "successfully."
        )
    else:
        recommendations.append(
            "Use this report as the starting benchmark "
            "for future website traffic comparisons."
        )

    search_ctr = (
        search_clicks
        / search_impressions
        * 100
        if search_impressions
        else 0
    )

    if search_impressions == 0:
        recommendations.append(
            "No Search Console impressions were "
            "recorded. Confirm the reporting period "
            "and Search Console collector status."
        )

    elif search_ctr < 2:
        recommendations.append(
            "Organic search visibility is present, but "
            f"CTR is {search_ctr:.2f}%. Review page "
            "titles and search descriptions for queries "
            "with high impressions and low clicks."
        )

    elif search_ctr >= 5:
        recommendations.append(
            f"Organic search CTR is {search_ctr:.2f}%, "
            "indicating strong click performance. "
            "Continue strengthening pages already "
            "ranking for relevant queries."
        )

    else:
        recommendations.append(
            f"Organic search CTR is {search_ctr:.2f}%. "
            "Prioritize high-impression queries where "
            "ranking or click-through performance can "
            "still improve."
        )

    if email_campaign_count == 0:
        recommendations.append(
            "No MailerLite campaigns were sent during "
            "this week."
        )
    else:
        recommendations.append(
            f"{email_campaign_count} MailerLite "
            "campaigns were included in the report. "
            f"Average open rate was "
            f"{email_open_rate:.2f}% and average click "
            f"rate was {email_click_rate:.2f}%."
        )

        if email_click_rate < 2:
            recommendations.append(
                "Review email calls to action, link "
                "placement and message relevance to "
                "support stronger click performance."
            )

    if website_sessions > website_views:
        recommendations.append(
            "Sessions are higher than recorded page "
            "views. Review the GA4 dimensions and "
            "metric combination to confirm that the "
            "reporting totals are being interpreted "
            "consistently."
        )

    return "\n\n".join(
        recommendations
    )


def generate_weekly_report(
    week_start=None,
):
    """
    Generate or update one WeeklyReport record.
    """

    if week_start is None:
        week_start, week_end = (
            get_previous_completed_week()
        )
    else:
        week_end = (
            week_start
            + timedelta(days=6)
        )

    system_log = SystemLog.objects.create(
        module="weekly_report_generator",
        status="running",
        records_processed=0,
    )

    try:
        website_totals = (
            WebsiteAnalytics.objects
            .filter(
                metric_date__range=(
                    week_start,
                    week_end,
                )
            )
            .aggregate(
                total_views=Sum("views"),
                total_sessions=Sum(
                    "sessions"
                ),
            )
        )

        search_totals = (
            SearchConsoleAnalytics.objects
            .filter(
                metric_date__range=(
                    week_start,
                    week_end,
                )
            )
            .aggregate(
                total_clicks=Sum("clicks"),
                total_impressions=Sum(
                    "impressions"
                ),
            )
        )

        website_views = int(
            website_totals.get(
                "total_views"
            )
            or 0
        )

        website_sessions = int(
            website_totals.get(
                "total_sessions"
            )
            or 0
        )

        search_clicks = int(
            search_totals.get(
                "total_clicks"
            )
            or 0
        )

        search_impressions = int(
            search_totals.get(
                "total_impressions"
            )
            or 0
        )

        campaign_snapshots = (
            get_mailerlite_campaign_snapshots(
                week_start,
                week_end,
            )
        )

        email_opens = sum(
            int(snapshot.opens or 0)
            for snapshot
            in campaign_snapshots
        )

        email_clicks = sum(
            int(snapshot.clicks or 0)
            for snapshot
            in campaign_snapshots
        )

        if campaign_snapshots:
            email_open_rate = (
                sum(
                    float(
                        snapshot.open_rate
                        or 0
                    )
                    for snapshot
                    in campaign_snapshots
                )
                / len(campaign_snapshots)
            )

            email_click_rate = (
                sum(
                    float(
                        snapshot.click_rate
                        or 0
                    )
                    for snapshot
                    in campaign_snapshots
                )
                / len(campaign_snapshots)
            )
        else:
            email_open_rate = 0
            email_click_rate = 0

        previous_report = (
            WeeklyReport.objects
            .filter(
                week_start=(
                    week_start
                    - timedelta(days=7)
                )
            )
            .first()
        )

        recommendations = (
            build_recommendations(
                website_views=website_views,
                website_sessions=(
                    website_sessions
                ),
                search_clicks=search_clicks,
                search_impressions=(
                    search_impressions
                ),
                email_campaign_count=len(
                    campaign_snapshots
                ),
                email_open_rate=(
                    email_open_rate
                ),
                email_click_rate=(
                    email_click_rate
                ),
                previous_report=(
                    previous_report
                ),
            )
        )

        with transaction.atomic():
            report, created = (
                WeeklyReport.objects
                .update_or_create(
                    week_start=week_start,
                    defaults={
                        "week_end": week_end,
                        "total_website_views": (
                            website_views
                        ),
                        "total_website_sessions": (
                            website_sessions
                        ),
                        "total_search_clicks": (
                            search_clicks
                        ),
                        "total_search_impressions": (
                            search_impressions
                        ),
                        "total_email_opens": (
                            email_opens
                        ),
                        "total_email_clicks": (
                            email_clicks
                        ),
                        "average_email_open_rate": (
                            round(
                                email_open_rate,
                                2,
                            )
                        ),
                        "average_email_click_rate": (
                            round(
                                email_click_rate,
                                2,
                            )
                        ),
                        "recommendations": (
                            recommendations
                        ),
                    },
                )
            )

        system_log.status = "success"
        system_log.records_processed = 1
        system_log.completed_at = (
            timezone.now()
        )

        system_log.save(
            update_fields=[
                "status",
                "records_processed",
                "completed_at",
            ]
        )

        return {
            "report": report,
            "created": created,
            "week_start": week_start,
            "week_end": week_end,
            "website_views": website_views,
            "website_sessions": (
                website_sessions
            ),
            "search_clicks": search_clicks,
            "search_impressions": (
                search_impressions
            ),
            "email_campaigns": len(
                campaign_snapshots
            ),
            "email_opens": email_opens,
            "email_clicks": email_clicks,
            "email_open_rate": round(
                email_open_rate,
                2,
            ),
            "email_click_rate": round(
                email_click_rate,
                2,
            ),
        }

    except Exception as error:
        system_log.status = "failed"
        system_log.error_message = str(error)
        system_log.completed_at = (
            timezone.now()
        )

        system_log.save(
            update_fields=[
                "status",
                "error_message",
                "completed_at",
            ]
        )

        raise