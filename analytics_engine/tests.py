from datetime import date, datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from google.analytics.data_v1beta.types import (
    DimensionValue,
    MetricValue,
    ResponseMetaData,
    Row,
    RunReportResponse,
)

from analytics_engine.collectors import ga4_db_collector, ga4_reports
from analytics_engine.collectors.ga4_db_collector import (
    collect_ga4_range,
    collect_ga4_to_database,
)
from analytics_engine.models import (
    GA4DailyMetric,
    GA4Report,
    GA4ReportRow,
    SystemLog,
    WeeklyReport,
)
from analytics_engine.services.website_dashboard import (
    build_website_dashboard_payload,
)
from analytics_engine.templatetags.ga4_format import (
    ga4_duration,
    ga4_percent,
)


START = date(2026, 9, 10)
END = date(2026, 9, 16)


# A tiny site where one user and one session span several pages and days:
#
#   Session A: user 1, 10 Sep, google / organic, views "/" and "/pipeline"
#   Session B: user 1, 11 Sep, (direct) / (none), views "/"
#   Session C: user 2, 11 Sep, (direct) / (none), views "/about-us"
#
# True totals: 4 views, 3 sessions, 2 users, 90s engagement.
SITE = {
    "screenPageViews": 4,
    "sessions": 3,
    "totalUsers": 2,
    "activeUsers": 2,
    "newUsers": 2,
    "engagedSessions": 2,
    "eventCount": 12,
    "userEngagementDuration": 90,
}

FIXTURE = {
    (): [((), SITE)],
    ("date",): [
        (("20260911",), {
            "screenPageViews": 2, "sessions": 2, "totalUsers": 2,
            "activeUsers": 2, "newUsers": 1, "engagedSessions": 1,
            "eventCount": 6, "userEngagementDuration": 40,
        }),
        (("20260910",), {
            "screenPageViews": 2, "sessions": 1, "totalUsers": 1,
            "activeUsers": 1, "newUsers": 1, "engagedSessions": 1,
            "eventCount": 6, "userEngagementDuration": 50,
        }),
    ],
    ("pagePath",): [
        (("/",), {
            "screenPageViews": 2, "sessions": 2, "activeUsers": 1,
            "eventCount": 6, "userEngagementDuration": 60,
        }),
        (("/pipeline",), {
            "screenPageViews": 1, "sessions": 1, "activeUsers": 1,
            "eventCount": 3, "userEngagementDuration": 20,
        }),
        (("/about-us",), {
            "screenPageViews": 1, "sessions": 1, "activeUsers": 1,
            "eventCount": 3, "userEngagementDuration": 10,
        }),
    ],
    ("pagePath", "pageTitle"): [
        (("/", "Medvolt - Home"), {"screenPageViews": 2}),
        (("/pipeline", "Pipeline"), {"screenPageViews": 1}),
        (("/about-us", "About us"), {"screenPageViews": 1}),
    ],
    ("sessionSourceMedium",): [
        (("(direct) / (none)",), {
            "sessions": 2, "engagedSessions": 1, "totalUsers": 2,
            "activeUsers": 2, "eventCount": 6,
            "userEngagementDuration": 40,
        }),
        (("google / organic",), {
            "sessions": 1, "engagedSessions": 1, "totalUsers": 1,
            "activeUsers": 1, "eventCount": 6,
            "userEngagementDuration": 50,
        }),
    ],
    ("sessionDefaultChannelGroup",): [
        (("Direct",), {"sessions": 2, "engagedSessions": 1}),
        (("Organic Search",), {"sessions": 1, "engagedSessions": 1}),
    ],
}


class FakeGA4Client:
    """Answers RunReportRequests from FIXTURE, like the GA4 Data API."""

    def __init__(self, fixture=FIXTURE, time_zone="Asia/Calcutta"):
        self.fixture = fixture
        self.time_zone = time_zone
        self.requests = []

    def run_report(self, request):
        self.requests.append(request)

        dimensions = tuple(d.name for d in request.dimensions)
        metrics = [m.name for m in request.metrics]
        rows = self.fixture[dimensions]

        page = rows[request.offset: request.offset + request.limit]

        return RunReportResponse(
            rows=[
                Row(
                    dimension_values=[
                        DimensionValue(value=value)
                        for value in dimension_values
                    ],
                    metric_values=[
                        MetricValue(value=str(values.get(name, 0)))
                        for name in metrics
                    ],
                )
                for dimension_values, values in page
            ],
            row_count=len(rows),
            metadata=ResponseMetaData(time_zone=self.time_zone),
        )


class GA4CollectionTestCase(TestCase):
    def collect(self, client=None, **kwargs):
        client = client or FakeGA4Client()
        collect_ga4_range(START, END, client=client, **kwargs)
        return client

    def payload(self):
        return build_website_dashboard_payload(START, END)


class SiteTotalsTests(GA4CollectionTestCase):
    def test_site_totals_come_from_summary_not_page_rows(self):
        self.collect()
        summary = self.payload()["summary"]

        self.assertEqual(summary["sessions"], 3)
        self.assertEqual(summary["total_users"], 2)
        self.assertEqual(summary["active_users"], 2)
        self.assertEqual(summary["new_users"], 2)

    def test_page_level_users_do_not_inflate_global_users(self):
        self.collect()
        payload = self.payload()

        page_user_sum = sum(p["active_users"] for p in payload["pages"])

        self.assertEqual(page_user_sum, 3)
        self.assertEqual(payload["summary"]["active_users"], 2)

    def test_page_level_sessions_are_not_stored_or_used(self):
        self.collect()

        page_rows = GA4ReportRow.objects.filter(
            report__report_type=GA4Report.REPORT_PAGE
        )

        self.assertTrue(page_rows.exists())
        self.assertFalse(page_rows.exclude(sessions=None).exists())
        self.assertEqual(self.payload()["summary"]["sessions"], 3)
        self.assertNotIn("sessions", self.payload()["pages"][0])

    def test_daily_users_are_not_summed_into_total_users(self):
        self.collect()
        payload = self.payload()

        daily_user_sum = sum(d["total_users"] for d in payload["daily"])

        self.assertEqual(daily_user_sum, 3)
        self.assertEqual(payload["summary"]["total_users"], 2)

    def test_daily_rows_come_from_date_query_in_order(self):
        self.collect()
        daily = self.payload()["daily"]

        self.assertEqual(
            [row["date"] for row in daily],
            [date(2026, 9, 10), date(2026, 9, 11)],
        )
        self.assertEqual(daily[1]["sessions"], 2)

    def test_views_match_summary_and_page_sum(self):
        self.collect()
        payload = self.payload()

        self.assertEqual(payload["summary"]["views"], 4)
        self.assertEqual(sum(p["views"] for p in payload["pages"]), 4)


class EngagementTests(GA4CollectionTestCase):
    def test_engagement_uses_aggregate_duration_over_site_sessions(self):
        self.collect()
        summary = self.payload()["summary"]

        # 90s / 3 sessions. The previous implementation divided by the
        # inflated page-row session sum (90 / 4 = 22.5).
        self.assertAlmostEqual(
            summary["avg_engagement_time_per_session"], 30.0
        )
        self.assertAlmostEqual(
            summary["avg_engagement_time_per_active_user"], 45.0
        )
        self.assertAlmostEqual(summary["engagement_rate"], 2 / 3)

    def test_page_engagement_is_per_active_user(self):
        self.collect()
        home = self.payload()["pages"][0]

        self.assertEqual(home["page_path"], "/")
        self.assertEqual(home["page_title"], "Medvolt - Home")
        self.assertAlmostEqual(
            home["avg_engagement_time_per_active_user"], 60.0
        )

    def test_ga4_duration_format_truncates_like_ga4(self):
        self.assertEqual(ga4_duration(20.74), "20s")
        self.assertEqual(ga4_duration(24.01), "24s")
        self.assertEqual(ga4_duration(73.5), "1m 13s")
        self.assertEqual(ga4_duration(None), "—")
        self.assertEqual(ga4_percent(0.31413), "31.4%")


class TrafficSourceTests(GA4CollectionTestCase):
    def test_source_sessions_sum_to_site_sessions(self):
        self.collect()
        payload = self.payload()
        sources = payload["traffic_sources"]

        self.assertEqual(
            [s["source_medium"] for s in sources],
            ["(direct) / (none)", "google / organic"],
        )
        self.assertEqual(
            sum(s["sessions"] for s in sources),
            payload["summary"]["sessions"],
        )
        self.assertAlmostEqual(
            sum(s["session_share"] for s in sources), 1.0
        )

    def test_channel_groups_are_separate_from_source_medium(self):
        self.collect()
        payload = self.payload()

        self.assertEqual(
            [c["channel_group"] for c in payload["channel_groups"]],
            ["Direct", "Organic Search"],
        )
        self.assertNotIn(
            "Direct",
            [s["source_medium"] for s in payload["traffic_sources"]],
        )


class DateRangeTests(GA4CollectionTestCase):
    def test_exact_inclusive_dates_are_sent_to_ga4(self):
        client = self.collect()

        for request in client.requests:
            self.assertEqual(request.date_ranges[0].start_date, "2026-09-10")
            self.assertEqual(request.date_ranges[0].end_date, "2026-09-16")

        self.assertEqual(self.payload()["period"]["days"], 7)

    def test_preset_last_7_days_ends_yesterday(self):
        self.assertEqual(
            ga4_reports.preset_range(7, date(2026, 9, 17)),
            (START, END),
        )

    def test_datetimes_are_rejected(self):
        with self.assertRaises(TypeError):
            ga4_reports.to_ga4_date(datetime(2026, 9, 10, 23, 0))

    def test_property_today_uses_property_timezone(self):
        # 20:00 UTC on 16 Sep is already 17 Sep in India.
        fixed_now = datetime(2026, 9, 16, 20, 0, tzinfo=ga4_reports.ZoneInfo("UTC"))

        with mock.patch.object(ga4_reports, "datetime") as fake_datetime:
            fake_datetime.now.side_effect = (
                lambda zone: fixed_now.astimezone(zone)
            )
            self.assertEqual(
                ga4_reports.property_today("Asia/Calcutta"),
                date(2026, 9, 17),
            )

    def test_ranges_are_not_mixed(self):
        self.collect()
        other_fixture = dict(FIXTURE)
        other_fixture[()] = [((), {**SITE, "sessions": 99})]
        collect_ga4_range(
            START, date(2026, 9, 17), client=FakeGA4Client(other_fixture)
        )

        self.assertEqual(self.payload()["summary"]["sessions"], 3)
        self.assertEqual(
            build_website_dashboard_payload(
                START, date(2026, 9, 17)
            )["summary"]["sessions"],
            99,
        )


class IdempotencyTests(GA4CollectionTestCase):
    def counts(self):
        return (
            GA4Report.objects.count(),
            GA4ReportRow.objects.count(),
            GA4DailyMetric.objects.count(),
        )

    def test_rerunning_collection_does_not_duplicate(self):
        self.collect()
        first_counts = self.counts()
        first_payload = self.payload()

        self.collect()
        self.collect()

        self.assertEqual(self.counts(), first_counts)
        self.assertEqual(self.counts(), (4, 1 + 3 + 2 + 2, 2))

        second_payload = self.payload()
        second_payload["period"].pop("last_synced")
        first_payload["period"].pop("last_synced")
        self.assertEqual(second_payload, first_payload)

    def test_recollection_replaces_rows_that_disappeared(self):
        self.collect()

        fixture = dict(FIXTURE)
        fixture[("pagePath",)] = FIXTURE[("pagePath",)][:1]
        self.collect(FakeGA4Client(fixture))

        self.assertEqual(
            [p["page_path"] for p in self.payload()["pages"]], ["/"]
        )

    def test_failed_fetch_leaves_existing_data_untouched(self):
        self.collect()

        broken = FakeGA4Client()
        broken.fixture = {k: v for k, v in FIXTURE.items() if k != ("pagePath",)}

        with self.assertRaises(KeyError):
            self.collect(broken)

        self.assertEqual(len(self.payload()["pages"]), 3)

    def test_full_run_collects_presets_and_logs_rows_written(self):
        client = FakeGA4Client()

        with mock.patch.object(
            ga4_db_collector, "create_ga4_client", return_value=client
        ), mock.patch.object(
            ga4_db_collector,
            "get_property_today",
            return_value=(date(2026, 9, 17), "Asia/Calcutta"),
        ):
            result = collect_ga4_to_database()
            collect_ga4_to_database()

        period_keys = set(
            GA4Report.objects.filter(
                report_type=GA4Report.REPORT_SUMMARY
            ).values_list("period_key", flat=True)
        )

        self.assertEqual(
            period_keys,
            {"week", "last_7_days", "last_30_days", "last_90_days"},
        )
        # Last 7 days ending 16 Sep == the benchmark range.
        self.assertTrue(
            GA4Report.objects.filter(
                period_key="last_7_days", start_date=START, end_date=END
            ).exists()
        )
        self.assertEqual(GA4Report.objects.count(), 4 * 4)

        log = SystemLog.objects.filter(module="ga4_collector").first()
        self.assertEqual(log.status, "success")
        self.assertEqual(log.records_processed, result["processed"])


class DashboardViewTests(GA4CollectionTestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("tester", password="x")
        self.client.force_login(user)

    def test_api_uses_explicit_metric_names(self):
        self.collect()

        with mock.patch("analytics_engine.views.collect_ga4_range") as fetch:
            response = self.client.get(
                reverse("dashboard:website_data"),
                {"start": "2026-09-10", "end": "2026-09-16"},
            )

        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)

        data = response.json()

        self.assertEqual(
            set(data),
            {"period", "summary", "daily", "traffic_sources",
             "channel_groups", "pages", "collector"},
        )
        self.assertEqual(data["summary"]["total_users"], 2)
        self.assertEqual(data["summary"]["sessions"], 3)
        self.assertNotIn("users", data["summary"])
        self.assertNotIn("users", data["daily"][0])
        self.assertEqual(data["period"]["start_date"], "2026-09-10")
        self.assertEqual(data["period"]["end_date"], "2026-09-16")

    def test_website_page_renders_ga4_labels_and_values(self):
        self.collect()

        response = self.client.get(
            reverse("dashboard:website"),
            {"start": "2026-09-10", "end": "2026-09-16"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["sessions"], 3)
        self.assertContains(response, "Total Users")
        self.assertContains(response, "Avg. Engagement / Session")
        self.assertContains(response, "Sessions by source / medium")
        self.assertContains(response, "Active Users")
        self.assertContains(response, "30s")
        self.assertNotContains(response, "Views by acquisition source")

    def test_uncollected_custom_range_is_fetched_on_demand(self):
        def fake_collect(start_date, end_date):
            collect_ga4_range(start_date, end_date, client=FakeGA4Client())

        with mock.patch(
            "analytics_engine.views.collect_ga4_range",
            side_effect=fake_collect,
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:website"),
                {"start": "2026-09-10", "end": "2026-09-16"},
            )

        fetch.assert_called_once_with(START, END)
        self.assertEqual(response.context["summary"]["total_users"], 2)

    def test_invalid_custom_range_is_rejected(self):
        response = self.client.get(
            reverse("dashboard:website"),
            {"start": "2026-09-16", "end": "2026-09-10"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["summary"])
        self.assertContains(response, "on or before")

    def test_preset_uses_latest_collected_preset_range(self):
        self.collect(period_key="last_7_days")

        response = self.client.get(reverse("dashboard:website"), {"days": "7"})

        self.assertEqual(response.context["start_date"], START)
        self.assertEqual(response.context["end_date"], END)
        self.assertEqual(response.context["summary"]["sessions"], 3)

    def test_overview_uses_summary_report(self):
        self.collect(period_key="last_30_days")

        response = self.client.get(reverse("dashboard:overview"))

        self.assertEqual(response.context["website_sessions"], 3)
        self.assertEqual(response.context["website_views"], 4)


class WeeklyReportTests(GA4CollectionTestCase):
    def test_weekly_report_uses_ga4_summary_for_the_week(self):
        from analytics_engine.services.weekly_report_service import (
            generate_weekly_report,
        )

        week_start = date(2026, 9, 7)
        week_end = date(2026, 9, 13)

        collect_ga4_range(week_start, week_end, client=FakeGA4Client())

        generate_weekly_report(week_start=week_start)

        report = WeeklyReport.objects.get(week_start=week_start)

        self.assertEqual(report.total_website_views, 4)
        self.assertEqual(report.total_website_sessions, 3)
