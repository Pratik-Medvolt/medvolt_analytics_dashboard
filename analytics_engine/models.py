from django.db import models
from django.utils import timezone


class Content(models.Model):
    content_id = models.CharField(
        max_length=100,
        primary_key=True,
    )

    platform = models.CharField(
        max_length=50,
        db_index=True,
    )

    content_type = models.CharField(
        max_length=50,
        db_index=True,
    )

    title = models.CharField(
        max_length=500,
    )

    website_url = models.URLField(
        max_length=1000,
        blank=True,
        default="",
    )

    canonical_url = models.URLField(
        max_length=1000,
        blank=True,
        default="",
    )

    external_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
    )

    publish_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
    )

    topic = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    author = models.CharField(
        max_length=255,
        blank=True,
        default="Medvolt",
    )

    status = models.CharField(
        max_length=50,
        default="published",
        db_index=True,
    )

    source = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "analytics_data_content"
        ordering = ["-publish_date", "title"]

    def __str__(self):
        return self.title


class WebsiteAnalytics(models.Model):
    """
    LEGACY: date x page x source rows from the previous GA4 collector.

    No longer written or read by the dashboard. Its sessions/users columns
    cannot be summed into site totals (see GA4Report below). Kept only to
    preserve historical data.
    """

    content = models.ForeignKey(
        Content,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="website_metrics",
    )

    metric_date = models.DateField(
        db_index=True,
    )

    page_url = models.URLField(
        max_length=1000,
        db_index=True,
    )

    page_title = models.CharField(
        max_length=500,
        blank=True,
        default="",
    )

    views = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)
    users = models.PositiveIntegerField(default=0)

    engagement_time_seconds = models.FloatField(
        default=0,
    )

    traffic_source = models.CharField(
        max_length=255,
        default="all",
    )

    collected_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "analytics_data_websiteanalytics"
        ordering = ["-metric_date", "-views"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "metric_date",
                    "page_url",
                    "traffic_source",
                ],
                name="unique_website_metric",
            )
        ]

    def __str__(self):
        return f"{self.page_title or self.page_url} - {self.metric_date}"


# ---------------------------------------------------------------------------
# GA4 reports
#
# Sessions and users are NON-ADDITIVE across dimensions: one session that
# visits "/", "/pipeline" and "/about-us" appears in three page rows, and one
# user active on two days appears in two date rows. Site totals therefore must
# come from a GA4 query WITHOUT those dimensions - never from summing rows.
#
# GA4DailyMetric  - one row per date (date dimension only). Only views, event
#                   count and engagement duration may be summed across days.
# GA4Report       - a report fetched for an exact date range; its rows are
#                   valid only for that range and must not be combined with
#                   rows from other ranges.
# ---------------------------------------------------------------------------


class GA4MetricFields(models.Model):
    """
    Raw GA4 metric values. NULL means the metric was not requested for
    that report (for example sessions on page rows).
    """

    views = models.PositiveIntegerField(null=True, blank=True)
    sessions = models.PositiveIntegerField(null=True, blank=True)
    total_users = models.PositiveIntegerField(null=True, blank=True)
    active_users = models.PositiveIntegerField(null=True, blank=True)
    new_users = models.PositiveIntegerField(null=True, blank=True)
    engaged_sessions = models.PositiveIntegerField(null=True, blank=True)
    event_count = models.PositiveIntegerField(null=True, blank=True)

    # GA4 userEngagementDuration, in seconds.
    engagement_duration_seconds = models.FloatField(
        null=True,
        blank=True,
    )

    class Meta:
        abstract = True

    @property
    def avg_engagement_time_per_session(self):
        """GA4 "Average engagement time per session"."""
        if not self.sessions or self.engagement_duration_seconds is None:
            return None
        return self.engagement_duration_seconds / self.sessions

    @property
    def avg_engagement_time_per_active_user(self):
        """GA4 "Average engagement time per active user"."""
        if not self.active_users or self.engagement_duration_seconds is None:
            return None
        return self.engagement_duration_seconds / self.active_users

    @property
    def engagement_rate(self):
        """GA4 engagementRate = engagedSessions / sessions."""
        if not self.sessions or self.engaged_sessions is None:
            return None
        return self.engaged_sessions / self.sessions


class GA4DailyMetric(GA4MetricFields):
    property_id = models.CharField(max_length=50, db_index=True)

    metric_date = models.DateField(db_index=True)

    collected_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_data_ga4dailymetric"
        ordering = ["metric_date"]

        constraints = [
            models.UniqueConstraint(
                fields=["property_id", "metric_date"],
                name="unique_ga4_daily_metric",
            )
        ]

    def __str__(self):
        return f"GA4 {self.property_id} {self.metric_date}"


class GA4Report(models.Model):
    REPORT_SUMMARY = "summary"
    REPORT_SOURCE_MEDIUM = "source_medium"
    REPORT_CHANNEL_GROUP = "channel_group"
    REPORT_PAGE = "page"

    REPORT_TYPE_CHOICES = [
        (REPORT_SUMMARY, "Site summary"),
        (REPORT_SOURCE_MEDIUM, "Sessions by source / medium"),
        (REPORT_CHANNEL_GROUP, "Sessions by default channel group"),
        (REPORT_PAGE, "Page performance"),
    ]

    property_id = models.CharField(max_length=50, db_index=True)

    report_type = models.CharField(
        max_length=30,
        choices=REPORT_TYPE_CHOICES,
    )

    start_date = models.DateField()
    end_date = models.DateField()

    # "last_7_days", "last_30_days", "last_90_days", "week" or "custom".
    period_key = models.CharField(
        max_length=30,
        default="custom",
        db_index=True,
    )

    property_timezone = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )

    row_count = models.PositiveIntegerField(default=0)

    collected_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_data_ga4report"
        ordering = ["-end_date", "report_type"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "property_id",
                    "report_type",
                    "start_date",
                    "end_date",
                ],
                name="unique_ga4_report_range",
            )
        ]

    def __str__(self):
        return (
            f"GA4 {self.report_type} "
            f"{self.start_date} to {self.end_date}"
        )


class GA4ReportRow(GA4MetricFields):
    report = models.ForeignKey(
        GA4Report,
        on_delete=models.CASCADE,
        related_name="rows",
    )

    # Raw GA4 dimension value (page path, "source / medium", channel
    # group). Empty for the summary report. Stored exactly as GA4 returns
    # it so values reconcile with the GA4 UI.
    dimension_value = models.CharField(
        max_length=2048,
        blank=True,
        default="",
    )

    page_title = models.CharField(
        max_length=500,
        blank=True,
        default="",
    )

    content = models.ForeignKey(
        Content,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ga4_report_rows",
    )

    class Meta:
        db_table = "analytics_data_ga4reportrow"
        ordering = ["report", "-sessions", "-views"]

        constraints = [
            models.UniqueConstraint(
                fields=["report", "dimension_value"],
                name="unique_ga4_report_row",
            )
        ]

    def __str__(self):
        return f"{self.report} - {self.dimension_value or 'total'}"


class SearchConsoleAnalytics(models.Model):
    """
    LEGACY: date x page x query rows from the previous Search Console
    collector.

    No longer written or read by the dashboard. Google omits anonymized
    queries from query rows and aggregates page rows by page, so summing
    these rows under-counts site clicks and impressions (see
    SearchConsoleReport below). Kept only to preserve historical data.
    """

    content = models.ForeignKey(
        Content,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="search_metrics",
    )

    metric_date = models.DateField(
        db_index=True,
    )

    page_url = models.URLField(
        max_length=1000,
        db_index=True,
    )

    query = models.CharField(
        max_length=1000,
        blank=True,
        default="",
    )

    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0)
    average_position = models.FloatField(default=0)

    collected_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "analytics_data_searchconsoleanalytics"
        ordering = ["-metric_date", "-clicks"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "metric_date",
                    "page_url",
                    "query",
                ],
                name="unique_search_metric",
            )
        ]

    def __str__(self):
        return f"{self.query or 'All queries'} - {self.metric_date}"


# ---------------------------------------------------------------------------
# Search Console reports
#
# Google aggregates each Search Analytics query differently:
#
# - Without page/query dimensions, results are aggregated byProperty and
#   include every click and impression.
# - Query rows omit anonymized (rare) queries, so their sum is lower than
#   the property total.
# - Page rows are aggregated byPage: one result showing two pages counts an
#   impression for each, so their impression sum is higher.
#
# Site totals therefore come only from a query without dimensions - never
# from summing query or page rows.
#
# SearchConsoleDailyMetric - one byProperty row per date (date dimension).
#                            Clicks and impressions may be summed across
#                            days; CTR and position may not be averaged.
# SearchConsoleReport      - a report fetched for an exact date range; its
#                            rows are valid only for that range.
#
# Google finalizes a date 2-3 days later. Until then the date is only
# available with dataState "all" and its values are preliminary. Every
# collector run re-fetches the recent window, so a preliminary row is
# replaced in place by Google's finalized values.
# ---------------------------------------------------------------------------


class SearchConsoleMetricFields(models.Model):
    """Raw Search Analytics values, stored exactly as Google returns them."""

    clicks = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)

    # Google's ratio (0-1). NULL when there were no impressions.
    ctr = models.FloatField(null=True, blank=True)

    # Google's average position. NULL when there were no impressions.
    position = models.FloatField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def ctr_percent(self):
        """CTR = clicks / impressions * 100, unrounded."""
        if not self.impressions:
            return None
        return self.clicks / self.impressions * 100


class SearchConsoleDailyMetric(SearchConsoleMetricFields):
    STATE_FINAL = "final"
    STATE_PRELIMINARY = "preliminary"

    DATA_STATE_CHOICES = [
        (STATE_FINAL, "Finalized"),
        (STATE_PRELIMINARY, "Preliminary (not yet finalized)"),
    ]

    site_url = models.CharField(max_length=255, db_index=True)
    search_type = models.CharField(max_length=20, default="web")

    # Whether Google had finalized this date when it was collected.
    data_state = models.CharField(
        max_length=12,
        choices=DATA_STATE_CHOICES,
        default=STATE_FINAL,
    )

    # Search Console date label (Pacific Time).
    metric_date = models.DateField(db_index=True)

    collected_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_data_gscdailymetric"
        ordering = ["metric_date"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "site_url",
                    "search_type",
                    "metric_date",
                ],
                name="unique_gsc_daily_date",
            )
        ]

    def __str__(self):
        return f"GSC {self.site_url} {self.metric_date}"


class SearchConsoleReport(models.Model):
    REPORT_SUMMARY = "summary"
    REPORT_QUERY = "query"
    REPORT_PAGE = "page"

    REPORT_TYPE_CHOICES = [
        (REPORT_SUMMARY, "Property summary"),
        (REPORT_QUERY, "Query performance"),
        (REPORT_PAGE, "Page performance"),
    ]

    site_url = models.CharField(max_length=255, db_index=True)
    search_type = models.CharField(max_length=20, default="web")

    # Search Analytics dataState used for every row of this report:
    # "final" when the whole range was finalized at collection time,
    # otherwise "all" (finalized plus preliminary dates).
    data_state = models.CharField(max_length=10, default="final")

    report_type = models.CharField(
        max_length=20,
        choices=REPORT_TYPE_CHOICES,
    )

    start_date = models.DateField()
    end_date = models.DateField()

    # "last_7_days", "last_30_days", "last_90_days", "week" or "custom".
    period_key = models.CharField(
        max_length=30,
        default="custom",
        db_index=True,
    )

    # responseAggregationType reported by Google (byProperty / byPage).
    aggregation_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
    )

    # Latest date in the range with finalized data when this report was
    # collected. NULL when no date in the range was finalized yet.
    data_through = models.DateField(null=True, blank=True)

    # Latest date in the range with preliminary data included in this
    # report ("all" reports only). Later dates had no Google data yet.
    preliminary_through = models.DateField(null=True, blank=True)

    row_count = models.PositiveIntegerField(default=0)

    collected_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_data_gscreport"
        ordering = ["-end_date", "report_type"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "site_url",
                    "search_type",
                    "report_type",
                    "start_date",
                    "end_date",
                ],
                name="unique_gsc_report_dates",
            )
        ]

    def __str__(self):
        return (
            f"GSC {self.report_type} "
            f"{self.start_date} to {self.end_date}"
        )

    @property
    def covered_through(self):
        """Latest date with any (final or preliminary) data included."""
        return self.preliminary_through or self.data_through


class SearchConsoleReportRow(SearchConsoleMetricFields):
    report = models.ForeignKey(
        SearchConsoleReport,
        on_delete=models.CASCADE,
        related_name="rows",
    )

    # Raw query text or page URL exactly as Google returns it. Empty for
    # the summary report.
    dimension_value = models.CharField(
        max_length=2048,
        blank=True,
        default="",
    )

    class Meta:
        db_table = "analytics_data_gscreportrow"
        ordering = ["report", "-clicks", "-impressions"]

        constraints = [
            models.UniqueConstraint(
                fields=["report", "dimension_value"],
                name="unique_gsc_report_row",
            )
        ]

    def __str__(self):
        return f"{self.report} - {self.dimension_value or 'total'}"


class MailerLiteAnalytics(models.Model):
    content = models.ForeignKey(
        Content,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mailerlite_metrics",
    )

    campaign_id = models.CharField(
        max_length=255,
        db_index=True,
    )

    subject = models.CharField(
        max_length=500,
    )

    sent_date = models.DateField(
        null=True,
        blank=True,
    )

    snapshot_date = models.DateField(
        default=timezone.localdate,
        db_index=True,
    )

    opens = models.PositiveIntegerField(default=0)
    open_rate = models.FloatField(default=0)

    clicks = models.PositiveIntegerField(default=0)
    click_rate = models.FloatField(default=0)

    unsubscribes = models.PositiveIntegerField(default=0)

    collected_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "analytics_data_mailerliteanalytics"
        ordering = ["-snapshot_date", "-sent_date"]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "campaign_id",
                    "snapshot_date",
                ],
                name="unique_mailerlite_snapshot",
            )
        ]

    def __str__(self):
        return f"{self.subject} - {self.snapshot_date}"


class WeeklyReport(models.Model):
    week_start = models.DateField(
        unique=True,
    )

    week_end = models.DateField()

    total_website_views = models.PositiveIntegerField(default=0)
    total_website_sessions = models.PositiveIntegerField(default=0)

    total_search_clicks = models.PositiveIntegerField(default=0)
    total_search_impressions = models.PositiveIntegerField(default=0)

    total_email_opens = models.PositiveIntegerField(default=0)
    total_email_clicks = models.PositiveIntegerField(default=0)

    average_email_open_rate = models.FloatField(default=0)
    average_email_click_rate = models.FloatField(default=0)

    recommendations = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        db_table = "analytics_data_weeklyreport"
        ordering = ["-week_start"]

    def __str__(self):
        return f"{self.week_start} to {self.week_end}"


class SystemLog(models.Model):
    module = models.CharField(
        max_length=100,
        db_index=True,
    )

    status = models.CharField(
        max_length=30,
        db_index=True,
    )

    records_processed = models.PositiveIntegerField(
        default=0,
    )

    error_message = models.TextField(
        blank=True,
        default="",
    )

    started_at = models.DateTimeField(
        default=timezone.now,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "analytics_data_systemlog"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.module}: {self.status}"