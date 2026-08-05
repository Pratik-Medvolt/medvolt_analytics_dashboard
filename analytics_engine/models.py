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


class SearchConsoleAnalytics(models.Model):
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