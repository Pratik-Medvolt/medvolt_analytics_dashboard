from datetime import date, datetime, timedelta
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import httplib2
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from googleapiclient.errors import HttpError

from analytics_engine.collectors import (
    search_console_db_collector,
    search_console_reports,
)
from analytics_engine.collectors.ga4_db_collector import collect_ga4_range
from analytics_engine.collectors.ga4_reports import GA4_PROPERTY_ID
from analytics_engine.collectors.search_console_db_collector import (
    collect_search_console_range,
    collect_search_console_to_database,
)
from analytics_engine.models import (
    Content,
    GA4Report,
    GA4ReportRow,
    SearchConsoleAnalytics,
    SearchConsoleDailyMetric,
    SearchConsoleReport,
    SearchConsoleReportRow,
    SystemLog,
    WeeklyReport,
)
from analytics_engine.services.search_console_dashboard import (
    build_search_console_payload,
    ctr_percent,
    is_range_current,
    page_lookup_key,
    resolve_page_titles,
)
from analytics_engine.tests import FakeGA4Client


START = date(2026, 9, 18)
END = date(2026, 9, 21)
TODAY = date(2026, 9, 24)


def day_metrics(clicks, impressions, position):
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions,
        "position": position,
    }


# Finalized property-level (byProperty) days.
FINAL_DAYS = {
    date(2026, 9, 17): day_metrics(11, 377, 13.684350132625994),
    date(2026, 9, 18): day_metrics(9, 292, 8.208904109589042),
    date(2026, 9, 19): day_metrics(5, 138, 9.123188405797102),
    date(2026, 9, 20): day_metrics(2, 132, 9.848484848484848),
    date(2026, 9, 21): day_metrics(7, 178, 7.696629213483146),
}

# Preliminary days, returned only for dataState "all".
FRESH_DAYS = {
    date(2026, 9, 22): day_metrics(11, 173, 8.965317919075144),
    date(2026, 9, 23): day_metrics(5, 154, 8.857142857142858),
}

# Query rows omit anonymized queries: 15 of the property's clicks.
QUERIES = [
    ("medvolt ai", day_metrics(8, 40, 1.2)),
    ("medvolt", day_metrics(5, 60, 2.1)),
    ("ai drug discovery", day_metrics(2, 71, 9.5)),
]

# Page rows are aggregated byPage: impressions exceed the property total.
PAGES = [
    ("https://www.medvolt.ai/", day_metrics(14, 400, 4.3)),
    ("https://www.medvolt.ai/about-us", day_metrics(6, 300, 6.0)),
    (
        "https://www.medvolt.ai/platform/"
        "medgraph-topaaz-for-small-molecule-new-chemical-entity",
        day_metrics(3, 288, 11.2),
    ),
]


def aggregate(days):
    clicks = sum(day["clicks"] for day in days)
    impressions = sum(day["impressions"] for day in days)

    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions,
        "position": (
            sum(day["position"] * day["impressions"] for day in days)
            / impressions
        ),
    }


class FakeRequest:
    def __init__(self, answer):
        self.answer = answer

    def execute(self):
        return self.answer()


class FakeGSCService:
    """Answers Search Analytics requests like the Search Console API."""

    def __init__(
        self,
        final_days=FINAL_DAYS,
        fresh_days=FRESH_DAYS,
        queries=QUERIES,
        pages=PAGES,
        site_urls=None,
        fail_on=None,
        placeholder_days=(TODAY,),
    ):
        self.final_days = final_days
        self.fresh_days = fresh_days
        self.queries = queries
        self.pages = pages
        self.site_urls = (
            site_urls
            if site_urls is not None
            else [search_console_reports.GSC_SITE_URL]
        )
        self.fail_on = fail_on
        # Dates Google has no data for yet: with dataState "all" it
        # returns a zero placeholder row for them (as it does for today).
        self.placeholder_days = placeholder_days
        self.requests = []

    def sites(self):
        service = self

        class Sites:
            def list(self):
                return FakeRequest(
                    lambda: {
                        "siteEntry": [
                            {
                                "siteUrl": url,
                                "permissionLevel": "siteFullUser",
                            }
                            for url in service.site_urls
                        ]
                    }
                )

        return Sites()

    def searchanalytics(self):
        service = self

        class SearchAnalytics:
            def query(self, siteUrl, body):
                return FakeRequest(lambda: service.answer(siteUrl, body))

        return SearchAnalytics()

    def answer(self, site_url, body):
        self.requests.append((site_url, dict(body)))

        dimensions = tuple(body.get("dimensions", ()))

        if dimensions == self.fail_on:
            raise HttpError(httplib2.Response({"status": 500}), b"backend")

        start = date.fromisoformat(body["startDate"])
        end = date.fromisoformat(body["endDate"])

        days = dict(self.final_days)
        fresh_in_range = []

        if body["dataState"] == "all":
            days.update(self.fresh_days)
            fresh_in_range = [
                day for day in self.fresh_days if start <= day <= end
            ]

        in_range = {
            day: metrics
            for day, metrics in sorted(days.items())
            if start <= day <= end
        }

        if dimensions == ():
            rows = [aggregate(in_range.values())] if in_range else []
        elif dimensions == ("date",):
            rows = [
                {"keys": [day.isoformat()], **metrics}
                for day, metrics in in_range.items()
            ]

            if body["dataState"] == "all":
                rows += [
                    {"keys": [day.isoformat()], "clicks": 0,
                     "impressions": 0, "ctr": 0, "position": 0}
                    for day in self.placeholder_days
                    if start <= day <= end and day not in in_range
                ]
        elif dimensions == ("query",):
            rows = [{"keys": [key], **m} for key, m in self.queries]
        elif dimensions == ("page",):
            rows = [{"keys": [key], **m} for key, m in self.pages]
        else:
            raise AssertionError(f"Unexpected dimensions {dimensions}")

        page = rows[body["startRow"]: body["startRow"] + body["rowLimit"]]

        response = {
            "responseAggregationType": (
                "byPage" if dimensions == ("page",) else "byProperty"
            ),
        }

        if page:
            response["rows"] = page

        if fresh_in_range and "date" in dimensions:
            response["metadata"] = {
                "firstIncompleteDate": min(fresh_in_range).isoformat()
            }

        return response

    def bodies(self, dimensions=None):
        return [
            body
            for _, body in self.requests
            if dimensions is None
            or tuple(body.get("dimensions", ())) == dimensions
        ]


class GSCTestCase(TestCase):
    def setUp(self):
        today_patch = mock.patch.object(
            search_console_db_collector,
            "dashboard_today",
            return_value=TODAY,
        )
        today_patch.start()
        self.addCleanup(today_patch.stop)

    def collect(self, service=None, start=START, end=END, **kwargs):
        service = service or FakeGSCService()
        collect_search_console_range(start, end, service=service, **kwargs)
        return service

    def payload(self, start=START, end=END):
        return build_search_console_payload(start, end)


class PropertySummaryTests(GSCTestCase):
    def test_kpis_come_from_property_summary(self):
        self.collect()
        summary = self.payload()["summary"]

        self.assertEqual(summary["clicks"], 23)
        self.assertEqual(summary["impressions"], 740)
        self.assertAlmostEqual(summary["ctr_percent"], 23 / 740 * 100)
        self.assertAlmostEqual(summary["ctr"], 23 / 740)

    def test_position_is_googles_aggregate_not_row_average(self):
        self.collect()
        summary = self.payload()["summary"]

        unweighted = sum(
            FINAL_DAYS[START + timedelta(days=offset)]["position"]
            for offset in range(4)
        ) / 4

        self.assertAlmostEqual(summary["position"], 8.548648648648648)
        self.assertNotAlmostEqual(summary["position"], unweighted, places=2)

    def test_ctr_is_not_an_average_of_daily_ctrs(self):
        self.collect()
        payload = self.payload()

        averaged = sum(
            row["ctr_percent"] for row in payload["daily"]
        ) / len(payload["daily"])

        self.assertAlmostEqual(payload["summary"]["ctr_percent"], 3.108108,
                               places=5)
        self.assertNotAlmostEqual(averaged, 3.108108, places=2)

    def test_summary_request_has_no_dimensions_and_is_by_property(self):
        service = self.collect()

        summary_bodies = [
            body
            for body in service.bodies(())
            if body["dataState"] == "final"
        ]

        self.assertEqual(len(summary_bodies), 1)
        self.assertNotIn("dimensions", summary_bodies[0])
        self.assertEqual(summary_bodies[0]["aggregationType"], "byProperty")

        report = SearchConsoleReport.objects.get(
            report_type=SearchConsoleReport.REPORT_SUMMARY
        )
        self.assertEqual(report.aggregation_type, "byProperty")

    def test_query_rows_are_not_used_as_property_totals(self):
        self.collect()
        payload = self.payload()

        self.assertEqual(sum(q["clicks"] for q in payload["queries"]), 15)
        self.assertEqual(payload["summary"]["clicks"], 23)

    def test_page_rows_are_not_used_as_property_totals(self):
        self.collect()
        payload = self.payload()

        self.assertEqual(
            sum(p["impressions"] for p in payload["pages"]), 988
        )
        self.assertEqual(payload["summary"]["impressions"], 740)

    def test_zero_impressions_are_handled(self):
        self.collect(FakeGSCService(final_days={}, fresh_days={}))
        summary = self.payload()["summary"]

        self.assertEqual(summary["clicks"], 0)
        self.assertEqual(summary["impressions"], 0)
        self.assertIsNone(summary["ctr_percent"])
        self.assertIsNone(summary["position"])
        self.assertIsNone(ctr_percent(0, 0))
        self.assertIsNone(ctr_percent(3, 0))

    def test_legacy_rows_are_not_read(self):
        SearchConsoleAnalytics.objects.create(
            metric_date=START,
            page_url="https://medvolt.ai/",
            query="noise",
            clicks=500,
            impressions=5000,
        )
        self.collect()

        self.assertEqual(self.payload()["summary"]["clicks"], 23)


class DailyTests(GSCTestCase):
    def test_daily_rows_match_date_report(self):
        self.collect()
        daily = self.payload()["daily"]

        self.assertEqual(
            [(row["date"], row["clicks"], row["impressions"])
             for row in daily],
            [
                (date(2026, 9, 18), 9, 292),
                (date(2026, 9, 19), 5, 138),
                (date(2026, 9, 20), 2, 132),
                (date(2026, 9, 21), 7, 178),
            ],
        )
        self.assertTrue(all(row["status"] == "final" for row in daily))

    def test_daily_sum_and_weighted_position_equal_summary(self):
        self.collect()
        payload = self.payload()
        daily = payload["daily"]

        impressions = sum(row["impressions"] for row in daily)
        weighted = sum(
            row["position"] * row["impressions"] for row in daily
        ) / impressions

        self.assertEqual(sum(row["clicks"] for row in daily), 23)
        self.assertEqual(impressions, 740)
        self.assertAlmostEqual(weighted, payload["summary"]["position"])

    def test_recent_dates_are_preliminary_then_pending_never_zero(self):
        self.collect(end=TODAY)
        payload = self.payload(end=TODAY)
        period = payload["period"]
        daily = {row["date"]: row for row in payload["daily"]}

        # The selected range is preserved.
        self.assertEqual(len(daily), 7)
        self.assertEqual((period["start_date"], period["end_date"]),
                         (START, TODAY))

        self.assertEqual(period["data_state"], "all")
        self.assertEqual(period["data_through"], END)
        self.assertEqual(
            (period["preliminary_start"], period["preliminary_end"]),
            (date(2026, 9, 22), date(2026, 9, 23)),
        )
        self.assertEqual(period["available_through"], date(2026, 9, 23))
        self.assertEqual(period["available_days"], 6)
        self.assertEqual(
            (period["pending_start"], period["pending_end"],
             period["pending_days"]),
            (TODAY, TODAY, 1),
        )

        self.assertEqual(daily[END]["status"], "final")
        self.assertEqual(daily[date(2026, 9, 22)]["status"], "preliminary")
        self.assertEqual(daily[date(2026, 9, 22)]["clicks"], 11)
        self.assertEqual(daily[TODAY]["status"], "pending")
        self.assertIsNone(daily[TODAY]["clicks"])
        self.assertIsNone(daily[TODAY]["impressions"])

        # KPIs include the same final + preliminary dates as the chart.
        self.assertEqual(payload["summary"]["clicks"], 23 + 11 + 5)
        self.assertEqual(payload["summary"]["impressions"],
                         740 + 173 + 154)
        self.assertEqual(
            payload["summary"]["clicks"],
            sum(row["clicks"] for row in payload["daily"]
                if row["status"] != "pending"),
        )

    def test_finalized_day_without_impressions_is_a_real_zero(self):
        final_days = dict(FINAL_DAYS)
        del final_days[date(2026, 9, 19)]

        self.collect(FakeGSCService(final_days=final_days))
        daily = {row["date"]: row for row in self.payload()["daily"]}

        self.assertEqual(daily[date(2026, 9, 19)]["status"], "final")
        self.assertEqual(daily[date(2026, 9, 19)]["clicks"], 0)

    def test_preliminary_rows_are_labelled_from_first_incomplete_date(self):
        self.collect(end=TODAY)

        states = dict(
            SearchConsoleDailyMetric.objects.values_list(
                "metric_date", "data_state"
            )
        )

        self.assertEqual(
            states,
            {
                date(2026, 9, 18): "final",
                date(2026, 9, 19): "final",
                date(2026, 9, 20): "final",
                date(2026, 9, 21): "final",
                date(2026, 9, 22): "preliminary",
                date(2026, 9, 23): "preliminary",
            },
        )
        self.assertFalse(
            SearchConsoleDailyMetric.objects.filter(metric_date=TODAY)
            .exists()
        )

    def test_zero_placeholder_row_for_today_is_pending_not_zero(self):
        service = self.collect(end=TODAY)

        placeholder = [
            row
            for _, body in list(service.requests)
            if body["dataState"] == "all"
            for row in service.answer(
                search_console_reports.GSC_SITE_URL, body
            ).get("rows", [])
            if row.get("keys") == [TODAY.isoformat()]
        ]
        self.assertTrue(placeholder)

        self.assertFalse(
            SearchConsoleDailyMetric.objects.filter(metric_date=TODAY)
            .exists()
        )
        daily = {row["date"]: row for row in self.payload(end=TODAY)["daily"]}
        self.assertEqual(daily[TODAY]["status"], "pending")
        self.assertIsNone(daily[TODAY]["clicks"])

    def test_finalized_dates_use_final_and_recent_dates_use_all(self):
        service = self.collect(end=TODAY)

        daily_bodies = [
            (body["dataState"], body["startDate"], body["endDate"])
            for body in service.bodies(("date",))
        ]

        # Freshness probe, finalized dates, then the unfinalized dates.
        self.assertEqual(
            daily_bodies,
            [
                ("all", "2026-09-14", "2026-09-24"),
                ("final", "2026-09-18", "2026-09-21"),
                ("all", "2026-09-22", "2026-09-24"),
            ],
        )

    def test_reports_of_a_range_share_one_data_state(self):
        mixed = self.collect(end=TODAY)
        final = self.collect()

        for service, expected in ((mixed, "all"), (final, "final")):
            for dimensions in ((), ("query",), ("page",)):
                states = {
                    body["dataState"]
                    for body in service.bodies(dimensions)
                }
                self.assertEqual(states, {expected}, dimensions)

        for report in SearchConsoleReport.objects.filter(end_date=TODAY):
            self.assertEqual(report.data_state, "all")
            self.assertEqual(report.preliminary_through, date(2026, 9, 23))

        for report in SearchConsoleReport.objects.filter(end_date=END):
            self.assertEqual(report.data_state, "final")
            self.assertIsNone(report.preliminary_through)

class QueryAndPageTests(GSCTestCase):
    def test_query_rows_are_stored_exactly_not_scaled(self):
        self.collect()
        queries = self.payload()["queries"]

        self.assertEqual(
            [(q["query"], q["clicks"], q["impressions"]) for q in queries],
            [("medvolt ai", 8, 40), ("medvolt", 5, 60),
             ("ai drug discovery", 2, 71)],
        )

    def test_page_report_is_by_page_and_keeps_raw_urls(self):
        service = self.collect()

        (page_body,) = service.bodies(("page",))
        self.assertEqual(page_body["aggregationType"], "byPage")

        pages = self.payload()["pages"]

        self.assertEqual(pages[0]["page_url"], "https://www.medvolt.ai/")
        self.assertEqual(pages[1]["page_url"],
                         "https://www.medvolt.ai/about-us")
        self.assertAlmostEqual(pages[0]["ctr_percent"], 14 / 400 * 100)

    def test_query_report_is_by_property(self):
        service = self.collect()

        (query_body,) = service.bodies(("query",))
        self.assertEqual(query_body["aggregationType"], "byProperty")

    def test_pagination_retrieves_all_rows(self):
        with mock.patch.object(search_console_reports, "GSC_ROW_LIMIT", 2):
            service = self.collect()

        query_bodies = service.bodies(("query",))

        self.assertEqual(
            [body["startRow"] for body in query_bodies], [0, 2]
        )
        self.assertEqual(len(self.payload()["queries"]), 3)


class RequestTests(GSCTestCase):
    def test_property_type_and_dates_are_sent_exactly(self):
        service = self.collect()

        for site_url, body in service.requests:
            self.assertEqual(site_url, search_console_reports.GSC_SITE_URL)
            self.assertEqual(body["type"], "web")

        for body in service.bodies():
            if body["dataState"] == "final":
                self.assertEqual(body["startDate"], "2026-09-18")
                self.assertEqual(body["endDate"], "2026-09-21")

        self.assertEqual(self.payload()["period"]["days"], 4)

    def test_unavailable_property_is_rejected(self):
        service = FakeGSCService(site_urls=["https://www.medvolt.ai/"])

        with mock.patch.object(
            search_console_reports, "GSC_SITE_URL", "sc-domain:medvolt.ai"
        ):
            with self.assertRaises(PermissionError):
                search_console_reports.validate_property_access(service)

    def test_datetimes_are_rejected(self):
        with self.assertRaises(TypeError):
            search_console_reports.to_gsc_date(datetime(2026, 9, 18, 23))

    def test_today_uses_pacific_time(self):
        # 03:00 UTC on 25 Sep is still 24 Sep in California, and
        # already 25 Sep in India.
        fixed_now = datetime(2026, 9, 25, 3, 0, tzinfo=ZoneInfo("UTC"))

        with mock.patch.object(
            search_console_reports, "datetime"
        ) as fake_datetime:
            fake_datetime.now.side_effect = (
                lambda zone: fixed_now.astimezone(zone)
            )
            self.assertEqual(
                search_console_reports.gsc_today(), date(2026, 9, 24)
            )

    def test_preset_is_last_n_calendar_days_including_today(self):
        self.assertEqual(
            search_console_reports.preset_range(7, TODAY),
            (date(2026, 9, 18), TODAY),
        )


class IdempotencyTests(GSCTestCase):
    def counts(self):
        return (
            SearchConsoleReport.objects.count(),
            SearchConsoleReportRow.objects.count(),
            SearchConsoleDailyMetric.objects.count(),
        )

    def test_rerunning_collection_does_not_duplicate(self):
        self.collect()
        first_counts = self.counts()
        first_payload = self.payload()

        self.collect()
        self.collect()

        self.assertEqual(self.counts(), first_counts)
        self.assertEqual(self.counts(), (3, 1 + 3 + 3, 4))

        second_payload = self.payload()
        for payload in (first_payload, second_payload):
            payload["period"].pop("last_synced")
            payload.pop("collector")
        self.assertEqual(second_payload, first_payload)

    def test_ranges_are_not_mixed(self):
        self.collect()
        self.collect(start=date(2026, 9, 17), end=END)

        self.assertEqual(self.payload()["summary"]["clicks"], 23)
        self.assertEqual(
            self.payload(start=date(2026, 9, 17))["summary"]["clicks"], 34
        )

    def test_failed_request_leaves_existing_data_untouched(self):
        self.collect()
        before = self.payload()

        with self.assertRaises(HttpError):
            self.collect(FakeGSCService(
                queries=[("changed", day_metrics(1, 1, 1.0))],
                fail_on=("page",),
            ))

        after = self.payload()
        self.assertEqual(after["queries"], before["queries"])
        self.assertEqual(after["summary"], before["summary"])

    def test_failed_full_run_is_logged_and_writes_nothing(self):
        with mock.patch.object(
            search_console_db_collector,
            "create_search_console_service",
            return_value=FakeGSCService(fail_on=("page",)),
        ):
            with self.assertRaises(HttpError):
                collect_search_console_to_database()

        self.assertEqual(SearchConsoleReport.objects.count(), 0)
        self.assertEqual(SearchConsoleDailyMetric.objects.count(), 0)

        log = SystemLog.objects.get(module="search_console_collector")
        self.assertEqual(log.status, "failed")
        self.assertIn("backend", log.error_message)

    def test_full_run_collects_presets_and_logs_rows_written(self):
        with mock.patch.object(
            search_console_db_collector,
            "create_search_console_service",
            return_value=FakeGSCService(),
        ):
            result = collect_search_console_to_database()
            collect_search_console_to_database()

        period_keys = set(
            SearchConsoleReport.objects.filter(
                report_type=SearchConsoleReport.REPORT_SUMMARY
            ).values_list("period_key", flat=True)
        )

        self.assertEqual(
            period_keys,
            {"week", "last_7_days", "last_30_days", "last_90_days"},
        )
        self.assertEqual(SearchConsoleReport.objects.count(), 4 * 3)

        seven_days = SearchConsoleReport.objects.get(
            report_type=SearchConsoleReport.REPORT_SUMMARY,
            period_key="last_7_days",
        )
        self.assertEqual(
            (
                seven_days.start_date,
                seven_days.end_date,
                seven_days.data_state,
                seven_days.data_through,
                seven_days.preliminary_through,
            ),
            (date(2026, 9, 18), TODAY, "all", END, date(2026, 9, 23)),
        )
        self.assertEqual(
            SearchConsoleDailyMetric.objects.count(),
            len(FINAL_DAYS) + len(FRESH_DAYS),
        )

        log = SystemLog.objects.filter(
            module="search_console_collector"
        ).first()
        self.assertEqual(log.status, "success")
        self.assertEqual(log.records_processed, result["processed"])
        self.assertEqual(result["latest_final_date"], END)
        self.assertEqual(result["latest_available_date"], date(2026, 9, 23))


class StalenessTests(GSCTestCase):
    def test_range_is_stale_once_newer_final_dates_exist(self):
        final_days = {
            day: metrics
            for day, metrics in FINAL_DAYS.items()
            if day <= date(2026, 9, 20)
        }
        self.collect(FakeGSCService(final_days=final_days, fresh_days={}))
        self.assertTrue(is_range_current(START, END))

        # A later run stores 21 Sep as finalized.
        SearchConsoleDailyMetric.objects.create(
            site_url=search_console_reports.GSC_SITE_URL,
            metric_date=END,
            clicks=7,
            impressions=178,
        )
        self.assertFalse(is_range_current(START, END))


class RecollectionTests(GSCTestCase):
    """A later run replaces preliminary values in place."""

    def full_run(self, service, today):
        with mock.patch.object(
            search_console_db_collector,
            "create_search_console_service",
            return_value=service,
        ), mock.patch.object(
            search_console_db_collector, "dashboard_today", return_value=today
        ):
            return collect_search_console_to_database()

    def next_day_service(self):
        # One day later: Google finalized 22 Sep with revised values,
        # 23 Sep is still preliminary (revised), 24 Sep is new.
        final_days = {
            **FINAL_DAYS,
            date(2026, 9, 22): day_metrics(12, 180, 8.9),
        }
        fresh_days = {
            date(2026, 9, 23): day_metrics(6, 160, 8.8),
            date(2026, 9, 24): day_metrics(3, 90, 9.0),
        }
        return FakeGSCService(final_days=final_days, fresh_days=fresh_days)

    def test_preliminary_values_are_replaced_by_final_without_duplicates(self):
        self.full_run(FakeGSCService(), TODAY)

        first = SearchConsoleDailyMetric.objects.get(
            metric_date=date(2026, 9, 22)
        )
        self.assertEqual((first.data_state, first.clicks),
                         ("preliminary", 11))

        self.full_run(self.next_day_service(), TODAY + timedelta(days=1))

        rows = SearchConsoleDailyMetric.objects.filter(
            metric_date=date(2026, 9, 22)
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().pk, first.pk)
        self.assertEqual(
            (rows.get().data_state, rows.get().clicks, rows.get().impressions),
            ("final", 12, 180),
        )

        revised = SearchConsoleDailyMetric.objects.get(
            metric_date=date(2026, 9, 23)
        )
        self.assertEqual((revised.data_state, revised.clicks),
                         ("preliminary", 6))

        # One row per date, whatever its state.
        dates = list(
            SearchConsoleDailyMetric.objects.values_list(
                "metric_date", flat=True
            )
        )
        self.assertEqual(len(dates), len(set(dates)))

    def test_rerunning_the_same_day_is_idempotent(self):
        self.full_run(FakeGSCService(), TODAY)
        counts = (
            SearchConsoleDailyMetric.objects.count(),
            SearchConsoleReport.objects.count(),
            SearchConsoleReportRow.objects.count(),
        )

        self.full_run(FakeGSCService(), TODAY)

        self.assertEqual(
            (
                SearchConsoleDailyMetric.objects.count(),
                SearchConsoleReport.objects.count(),
                SearchConsoleReportRow.objects.count(),
            ),
            counts,
        )

    def test_preliminary_date_google_drops_is_removed_not_kept(self):
        self.full_run(FakeGSCService(), TODAY)

        fresh_days = {date(2026, 9, 22): FRESH_DAYS[date(2026, 9, 22)]}
        self.full_run(FakeGSCService(fresh_days=fresh_days), TODAY)

        self.assertFalse(
            SearchConsoleDailyMetric.objects.filter(
                metric_date=date(2026, 9, 23)
            ).exists()
        )
        payload = self.payload(end=TODAY)
        daily = {row["date"]: row for row in payload["daily"]}
        self.assertEqual(daily[date(2026, 9, 23)]["status"], "pending")
        self.assertEqual(payload["summary"]["clicks"], 23 + 11)

    def test_earlier_preset_gets_revised_preliminary_values(self):
        self.full_run(FakeGSCService(), TODAY)

        # Same day, later: Google revised 23 Sep's preliminary values.
        fresh_days = {
            **FRESH_DAYS,
            date(2026, 9, 23): day_metrics(6, 203, 8.6),
        }
        self.full_run(FakeGSCService(fresh_days=fresh_days),
                      TODAY + timedelta(days=1))

        # Yesterday's 7-day preset (18-24) still includes unfinalized
        # dates, so it was refreshed and keeps its preset label.
        report = SearchConsoleReport.objects.get(
            report_type=SearchConsoleReport.REPORT_SUMMARY,
            start_date=START,
            end_date=TODAY,
        )
        self.assertEqual(report.period_key, "last_7_days")
        self.assertEqual(report.rows.get().impressions, 740 + 173 + 203)
        self.assertEqual(
            SearchConsoleDailyMetric.objects.get(
                metric_date=date(2026, 9, 23)
            ).impressions,
            203,
        )

    def test_range_is_stale_after_preliminary_rows_are_recollected(self):
        self.collect(end=TODAY)
        self.assertTrue(is_range_current(START, TODAY))

        # A later run re-fetches the preliminary days (possibly revised)
        # without refreshing this range's report.
        for row in SearchConsoleDailyMetric.objects.filter(
            data_state="preliminary"
        ):
            row.save()

        self.assertFalse(is_range_current(START, TODAY))

        # A finalized-only range is unaffected.
        self.collect()
        self.assertTrue(is_range_current(START, END))

    def test_custom_range_is_refreshed_until_final(self):
        self.collect(end=TODAY)

        self.full_run(self.next_day_service(), TODAY + timedelta(days=1))

        report = SearchConsoleReport.objects.get(
            report_type=SearchConsoleReport.REPORT_SUMMARY,
            start_date=START,
            end_date=TODAY,
        )
        self.assertEqual(report.data_through, date(2026, 9, 22))
        self.assertEqual(report.preliminary_through, TODAY)
        self.assertEqual(report.rows.get().clicks, 23 + 12 + 6 + 3)


class PageTitleTests(TestCase):
    def setUp(self):
        report = GA4Report.objects.create(
            property_id=GA4_PROPERTY_ID,
            report_type=GA4Report.REPORT_PAGE,
            start_date=date(2026, 6, 26),
            end_date=TODAY,
            period_key="last_90_days",
        )

        for path, title in [
            ("/", "Medvolt | AI Platform for Drug Discovery"),
            ("/about-us", "About Us | Medvolt.ai"),
            ("/pipeline", "Pipeline | Medvolt.ai"),
        ]:
            GA4ReportRow.objects.create(
                report=report, dimension_value=path, page_title=title
            )

        # Legacy record whose title belongs to another page.
        Content.objects.create(
            content_id="WEB_LEGACY",
            platform="website",
            content_type="page",
            title="Pipeline | Medvolt.ai",
            website_url="https://medvolt.ai/platform/medgraph-topaaz",
            canonical_url="https://medvolt.ai/platform/medgraph-topaaz",
            external_id="/platform/medgraph-topaaz",
            source="ga4_api",
        )

        Content.objects.create(
            content_id="WEB_BLOG",
            platform="website",
            content_type="blog",
            title="Protein Flexibility",
            website_url="https://medvolt.ai/blog/protein-flexibility-old",
            canonical_url="https://medvolt.ai/blog/protein-flexibility",
            external_id="/blog/protein-flexibility",
            source="content_discovery",
        )

    def resolve(self, url):
        return resolve_page_titles([url])[url]

    def test_www_and_trailing_slash_match_the_same_page(self):
        for url in (
            "https://www.medvolt.ai/about-us",
            "https://medvolt.ai/about-us/",
            "http://www.medvolt.ai/about-us",
        ):
            self.assertEqual(self.resolve(url), "About Us | Medvolt.ai")

    def test_query_parameters_are_ignored_for_lookup_only(self):
        url = "https://www.medvolt.ai/about-us?utm_source=x#team"

        self.assertEqual(self.resolve(url), "About Us | Medvolt.ai")
        self.assertEqual(page_lookup_key(url), ("medvolt.ai", "/about-us"))

    def test_canonical_url_matches(self):
        self.assertEqual(
            self.resolve("https://www.medvolt.ai/blog/protein-flexibility/"),
            "Protein Flexibility",
        )

    def test_similar_paths_do_not_share_titles(self):
        self.assertIsNone(self.resolve("https://medvolt.ai/pipeline-v2"))
        self.assertIsNone(
            self.resolve("https://medvolt.ai/blog/protein-flexibility-2")
        )
        self.assertIsNone(self.resolve("https://medvolt.ai/About-Us"))

    def test_unrelated_legacy_title_is_not_used(self):
        self.assertIsNone(
            self.resolve("https://www.medvolt.ai/platform/medgraph-topaaz")
        )

    def test_other_hosts_do_not_borrow_site_titles(self):
        self.assertIsNone(self.resolve("https://app.medvolt.ai/"))
        self.assertIsNone(self.resolve("https://app.medvolt.ai/about-us"))

    def test_homepage_article_and_platform_pages_get_their_own_title(self):
        GA4ReportRow.objects.create(
            report=GA4Report.objects.get(),
            dimension_value="/blog/meta-omol25-uma-models",
            page_title="Meta's OMol25 | Medvolt.ai",
        )
        GA4ReportRow.objects.create(
            report=GA4Report.objects.get(),
            dimension_value="/platform/medgraph-topaaz-for-small-molecule",
            page_title="MedGraph-Topaaz | Medvolt.ai",
        )

        self.assertEqual(
            self.resolve("https://www.medvolt.ai/"),
            "Medvolt | AI Platform for Drug Discovery",
        )
        self.assertEqual(
            self.resolve("https://medvolt.ai"),
            "Medvolt | AI Platform for Drug Discovery",
        )
        self.assertEqual(
            self.resolve("https://www.medvolt.ai/pipeline"),
            "Pipeline | Medvolt.ai",
        )
        self.assertEqual(
            self.resolve("https://www.medvolt.ai/blog/meta-omol25-uma-models"),
            "Meta's OMol25 | Medvolt.ai",
        )
        self.assertEqual(
            self.resolve(
                "https://www.medvolt.ai/platform/"
                "medgraph-topaaz-for-small-molecule/"
            ),
            "MedGraph-Topaaz | Medvolt.ai",
        )

    def test_percent_encoded_path_matches_ga4_decoded_path_only(self):
        GA4ReportRow.objects.create(
            report=GA4Report.objects.get(),
            dimension_value="/blog/periodic-table to-ai-design",
            page_title="Periodic Table to AI | Medvolt.ai",
        )

        self.assertEqual(
            self.resolve("https://www.medvolt.ai/blog/periodic-table%20to-ai-design"),
            "Periodic Table to AI | Medvolt.ai",
        )
        # A different URL that merely looks similar gets no title.
        self.assertIsNone(
            self.resolve("https://www.medvolt.ai/blog/periodic-table-to-ai-design")
        )

    def test_pdf_resources_without_a_title_show_their_path(self):
        url = (
            "https://www.medvolt.ai/whitepapers/"
            "medgraph-topaaz-whitepaper.pdf"
        )

        # The Topaaz platform page title must not leak onto its PDF.
        self.assertIsNone(self.resolve(url))

    def test_unknown_url_falls_back_to_its_path_on_the_dashboard(self):
        report = SearchConsoleReport.objects.create(
            site_url=search_console_reports.GSC_SITE_URL,
            report_type=SearchConsoleReport.REPORT_PAGE,
            start_date=START,
            end_date=END,
        )
        SearchConsoleReportRow.objects.create(
            report=report,
            dimension_value="https://www.medvolt.ai/unknown/",
            clicks=1,
            impressions=10,
        )

        (page,) = build_search_console_payload(START, END)["pages"]

        self.assertIsNone(page["page_title"])
        self.assertEqual(page["page_path"], "/unknown")
        self.assertEqual(page["page_url"], "https://www.medvolt.ai/unknown/")


class DashboardViewTests(GSCTestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("tester", password="x")
        self.client.force_login(user)

        views_today = mock.patch(
            "analytics_engine.views.dashboard_today", return_value=TODAY
        )
        views_today.start()
        self.addCleanup(views_today.stop)

    def test_page_renders_property_kpis_with_presentation_rounding(self):
        self.collect()

        with mock.patch(
            "analytics_engine.views.collect_search_console_range"
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-18", "end": "2026-09-21"},
            )

        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["clicks"], 23)
        self.assertContains(response, 'data-kpi="clicks">23<')
        self.assertContains(response, 'data-kpi="impressions">740<')
        self.assertContains(response, 'data-kpi="ctr">3.11%<')
        self.assertContains(response, 'data-kpi="position">8.55<')
        self.assertContains(response, "may exclude anonymized search terms")
        self.assertContains(response, "Data available through")
        # Every selected date is final: no "(x of y days)" suffix.
        self.assertNotContains(response, "sc-freshness-pending")

    def test_pending_dates_are_explained_and_charted_as_gaps(self):
        self.collect(end=TODAY)

        with mock.patch(
            "analytics_engine.views.collect_search_console_range"
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-18", "end": "2026-09-24"},
            )

        # Nothing newer has been stored since, so no refetch.
        fetch.assert_not_called()

        self.assertContains(response, "Data available through 23 Sep")
        self.assertContains(
            response, "(22 Sep–23 Sep preliminary · 6 of 7 days)"
        )
        self.assertContains(response, "preliminary (dashed)")
        self.assertContains(response, "awaiting Google data (not zero)")
        self.assertEqual(response.context["period"]["available_days"], 6)
        self.assertEqual(response.context["period"]["pending_days"], 1)
        self.assertEqual(
            response.context["daily_status"],
            ["final"] * 4 + ["preliminary"] * 2 + ["pending"],
        )

        # The long explanation lives only in the collapsible details.
        header = response.content.decode().split('class="sc-kpi-grid"')[0]
        self.assertNotIn("sc-note", header)
        self.assertNotIn("Presets are the last", header)
        self.assertEqual(
            response.context["daily_clicks"], [9, 5, 2, 7, 11, 5, None]
        )
        self.assertEqual(response.context["summary"]["impressions"],
                         740 + 173 + 154)

    def test_uncollected_custom_range_is_fetched_on_demand(self):
        def fake_collect(start_date, end_date, **kwargs):
            collect_search_console_range(
                start_date, end_date, service=FakeGSCService(), **kwargs
            )

        with mock.patch(
            "analytics_engine.views.collect_search_console_range",
            side_effect=fake_collect,
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-18", "end": "2026-09-21"},
            )

        fetch.assert_called_once_with(START, END, period_key="custom")
        self.assertEqual(response.context["summary"]["clicks"], 23)

    def test_invalid_custom_ranges_are_rejected(self):
        for params, message in [
            ({"start": "2026-09-21", "end": "2026-09-18"}, "on or before"),
            ({"start": "2026-09-18", "end": "2026-09-30"}, "after today"),
            ({"start": "2020-01-01", "end": "2020-01-02"}, "16 months"),
        ]:
            response = self.client.get(
                reverse("dashboard:search_console"), params
            )

            self.assertIsNone(response.context["summary"])
            self.assertContains(response, message)

    def test_preset_ends_today_and_uses_stored_range(self):
        self.collect(start=date(2026, 9, 18), end=TODAY,
                     period_key="last_7_days")

        with mock.patch(
            "analytics_engine.views.collect_search_console_range"
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:search_console"), {"days": "7"}
            )

        fetch.assert_not_called()

        self.assertEqual(response.context["start_date"], START)
        self.assertEqual(response.context["end_date"], TODAY)
        self.assertEqual(response.context["summary"]["clicks"],
                         23 + 11 + 5)

    def test_presets_are_calendar_days_ending_today(self):
        """On 25 Sep: 7 Days = 19-25, 30 Days = 27 Aug-25, 90 = 28 Jun-25."""

        next_day = TODAY + timedelta(days=1)

        def fake_collect(start_date, end_date, **kwargs):
            collect_search_console_range(
                start_date, end_date, service=FakeGSCService(), **kwargs
            )

        expected = {
            "7": date(2026, 9, 19),
            "30": date(2026, 8, 27),
            "90": date(2026, 6, 28),
        }

        with mock.patch(
            "analytics_engine.views.dashboard_today", return_value=next_day
        ), mock.patch(
            "analytics_engine.views.collect_search_console_range",
            side_effect=fake_collect,
        ) as fetch:
            for days, start in expected.items():
                response = self.client.get(
                    reverse("dashboard:search_console"), {"days": days}
                )

                self.assertEqual(response.context["start_date"], start)
                self.assertEqual(response.context["end_date"], next_day)
                self.assertEqual(
                    response.context["period"]["days"], int(days)
                )
                fetch.assert_called_with(
                    start, next_day, period_key=f"last_{days}_days"
                )

                # 25 Sep has no Google data yet: pending, not zero.
                daily = response.context["period"]
                self.assertEqual(daily["pending_end"], next_day)
                self.assertIsNone(response.context["daily_clicks"][-1])

        self.assertEqual(
            SearchConsoleReport.objects.get(
                report_type=SearchConsoleReport.REPORT_SUMMARY,
                start_date=date(2026, 9, 19),
                end_date=next_day,
            ).period_key,
            "last_7_days",
        )

    def test_preset_fetch_failure_is_reported_not_shifted(self):
        with mock.patch(
            "analytics_engine.views.collect_search_console_range",
            side_effect=RuntimeError("quota exceeded"),
        ):
            response = self.client.get(
                reverse("dashboard:search_console"), {"days": "7"}
            )

        self.assertIsNone(response.context["summary"])
        self.assertEqual(response.context["end_date"], TODAY)
        self.assertContains(response, "quota exceeded")

    def test_json_endpoint(self):
        self.collect()

        response = self.client.get(
            reverse("dashboard:search_console_data"),
            {"start": "2026-09-18", "end": "2026-09-21"},
        )

        data = response.json()

        self.assertEqual(
            set(data),
            {"period", "summary", "daily", "queries", "pages", "collector"},
        )
        self.assertEqual(data["summary"]["clicks"], 23)
        self.assertEqual(data["period"]["search_type"], "web")
        self.assertEqual(data["period"]["data_state"], "final")
        self.assertEqual(len(data["daily"]), 4)

    def test_collector_status_reports_rows_written_not_traffic(self):
        with mock.patch.object(
            search_console_db_collector,
            "create_search_console_service",
            return_value=FakeGSCService(),
        ):
            result = collect_search_console_to_database()

        response = self.client.get(
            reverse("dashboard:search_console"), {"days": "7"}
        )

        collector = response.context["collector"]

        self.assertEqual(collector["status"], "success")
        self.assertEqual(collector["rows_written"], result["processed"])
        self.assertIsNotNone(collector["last_success_at"])
        self.assertContains(response, "<details")
        self.assertContains(response, "Data Status &amp; Collection Details")
        self.assertContains(response, "Records processed")
        self.assertContains(response, "not a traffic metric")
        self.assertContains(response, "sc-domain:" if search_console_reports.GSC_SITE_URL.startswith("sc-domain:") else "Property")
        self.assertContains(response, "America/Los_Angeles")

    def test_overview_uses_property_summary(self):
        self.collect(start=date(2026, 8, 26), end=TODAY,
                     period_key="last_30_days")
        collect_ga4_range(
            date(2026, 9, 10), date(2026, 9, 16), client=FakeGA4Client(),
            period_key="last_30_days",
        )

        response = self.client.get(reverse("dashboard:overview"))

        self.assertEqual(response.context["overview_search_clicks"],
                         34 + 11 + 5)
        self.assertEqual(response.context["search_clicks"], 34 + 11 + 5)
        self.assertEqual(response.context["website_sessions"], 3)


class WeeklyReportTests(GSCTestCase):
    def test_weekly_report_uses_search_console_property_totals(self):
        from analytics_engine.services.weekly_report_service import (
            generate_weekly_report,
        )

        week_start = date(2026, 9, 14)
        week_end = date(2026, 9, 20)

        collect_ga4_range(week_start, week_end, client=FakeGA4Client())
        self.collect(start=week_start, end=week_end, period_key="week")

        generate_weekly_report(week_start=week_start)

        report = WeeklyReport.objects.get(week_start=week_start)

        # 17-20 Sep property totals, not summed query or page rows.
        self.assertEqual(report.total_search_clicks, 11 + 9 + 5 + 2)
        self.assertEqual(report.total_search_impressions,
                         377 + 292 + 138 + 132)
        self.assertEqual(report.total_website_sessions, 3)


class VerifyCommandTests(GSCTestCase):
    def run_verify(self, service, *args):
        out = StringIO()

        with mock.patch(
            "analytics_engine.management.commands.verify_gsc."
            "create_search_console_service",
            return_value=service,
        ), mock.patch(
            "analytics_engine.management.commands.verify_gsc.dashboard_today",
            return_value=TODAY,
        ), mock.patch(
            "analytics_engine.views.dashboard_today", return_value=TODAY
        ):
            call_command(
                "verify_gsc",
                "--start", "2026-09-18",
                "--end", "2026-09-21",
                *args,
                stdout=out,
            )

        return out.getvalue()

    def test_default_mode_is_read_only_and_flags_missing_data(self):
        with self.assertRaises(CommandError):
            self.run_verify(FakeGSCService())

        self.assertEqual(SearchConsoleReport.objects.count(), 0)
        self.assertEqual(SystemLog.objects.count(), 0)

    def test_passes_when_api_database_and_dashboard_agree(self):
        self.collect()

        output = self.run_verify(FakeGSCService())

        self.assertIn("PASS", output)
        self.assertNotIn("MISMATCH", output)
        self.assertIn("KPI card [clicks]", output)
        self.assertIn("Daily chart [clicks] (4 values, 0 gaps)", output)
        self.assertIn("QUERIES: ALL 3 API ROWS", output)
        self.assertIn("3 rows x 4 metrics = 12 values compared; 0 row(s)",
                      output)
        self.assertIn("Top Queries chart = first 10 query rows", output)
        self.assertIn("Page table = first 3 page rows", output)

    def test_fails_when_stored_values_differ(self):
        self.collect()
        SearchConsoleReportRow.objects.filter(
            report__report_type=SearchConsoleReport.REPORT_SUMMARY
        ).update(clicks=15)

        with self.assertRaises(CommandError):
            self.run_verify(FakeGSCService())

    def test_every_query_row_is_compared_not_only_the_top(self):
        self.collect()
        last_query = SearchConsoleReportRow.objects.get(
            report__report_type=SearchConsoleReport.REPORT_QUERY,
            dimension_value="ai drug discovery",
        )
        last_query.impressions = 999
        last_query.save()

        with self.assertRaises(CommandError):
            self.run_verify(FakeGSCService(), "--top", "1")

    def test_duplicate_or_extra_stored_rows_fail(self):
        self.collect()
        report = SearchConsoleReport.objects.get(
            report_type=SearchConsoleReport.REPORT_PAGE
        )
        SearchConsoleReportRow.objects.create(
            report=report,
            dimension_value="https://www.medvolt.ai/not-returned",
            clicks=1,
            impressions=1,
        )

        with self.assertRaises(CommandError):
            self.run_verify(FakeGSCService())

    def test_collect_flag_refreshes_before_verifying(self):
        output = self.run_verify(FakeGSCService(), "--collect")

        self.assertIn("PASS", output)
        self.assertEqual(SearchConsoleReport.objects.count(), 3)


class VerifyRenderedTests(VerifyCommandTests):
    def test_rendered_check_covers_pending_gaps(self):
        self.collect(end=TODAY)
        out = StringIO()

        with mock.patch(
            "analytics_engine.management.commands.verify_gsc."
            "create_search_console_service",
            return_value=FakeGSCService(),
        ), mock.patch(
            "analytics_engine.management.commands.verify_gsc.dashboard_today",
            return_value=TODAY,
        ), mock.patch(
            "analytics_engine.views.dashboard_today", return_value=TODAY
        ):
            call_command(
                "verify_gsc", "--start", "2026-09-18", "--end", "2026-09-24",
                stdout=out,
            )

        output = out.getvalue()
        self.assertIn("Daily chart [clicks] (6 values, 1 gaps)", output)
        self.assertIn("2026-09-22 [status]", output)
        self.assertIn("1 pending", output)
        self.assertIn("PASS", output)

    def test_rendered_kpi_mismatch_fails(self):
        self.collect()

        with mock.patch(
            "analytics_engine.management.commands.verify_gsc.round_half_up",
            return_value="0.00",
        ):
            with self.assertRaises(CommandError):
                self.run_verify(FakeGSCService())


def at_utc(*args):
    """Freeze search_console_reports' clock at a UTC instant."""

    fixed_now = datetime(*args, tzinfo=ZoneInfo("UTC"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz)

    patcher = mock.patch.object(
        search_console_reports, "datetime", FrozenDatetime
    )
    patcher.start()
    return patcher


class DashboardTimezoneTests(TestCase):
    """
    "Today" for the presets is the India calendar day, while Google's
    date labels stay in Pacific Time and Django's TIME_ZONE stays UTC.
    """

    def today_at(self, *utc):
        patcher = at_utc(*utc)
        try:
            return (
                search_console_reports.dashboard_today(),
                search_console_reports.gsc_today(),
            )
        finally:
            patcher.stop()

    def test_default_reporting_timezone_is_india(self):
        self.assertEqual(
            search_console_reports.DASHBOARD_TIME_ZONE, "Asia/Kolkata"
        )

    def test_early_morning_ist_is_already_the_new_day(self):
        cases = [
            # (UTC instant, IST time)                  dashboard, Google
            ((2026, 9, 24, 18, 29), date(2026, 9, 24), date(2026, 9, 24)),  # 23:59 IST
            ((2026, 9, 24, 18, 30), date(2026, 9, 25), date(2026, 9, 24)),  # 00:00 IST
            ((2026, 9, 24, 19, 0), date(2026, 9, 25), date(2026, 9, 24)),   # 00:30 IST
            ((2026, 9, 24, 23, 59), date(2026, 9, 25), date(2026, 9, 24)),  # 05:29 IST
            ((2026, 9, 25, 0, 0), date(2026, 9, 25), date(2026, 9, 24)),    # 05:30 IST
            ((2026, 9, 25, 7, 0), date(2026, 9, 25), date(2026, 9, 25)),    # 12:30 IST
        ]

        for utc, dashboard_day, google_day in cases:
            with self.subTest(utc=utc):
                self.assertEqual(self.today_at(*utc),
                                 (dashboard_day, google_day))

    def test_django_time_zone_is_unchanged(self):
        from django.conf import settings

        self.assertEqual(settings.TIME_ZONE, "UTC")

    def test_reporting_timezone_is_configurable(self):
        with mock.patch.object(
            search_console_reports, "DASHBOARD_TIME_ZONE", "UTC"
        ):
            self.assertEqual(self.today_at(2026, 9, 24, 19, 0)[0],
                             date(2026, 9, 24))

    def test_presets_are_exactly_n_inclusive_days_ending_today(self):
        today = self.today_at(2026, 9, 24, 19, 0)[0]  # 00:30 IST 25 Sep

        for days, start in [
            (7, date(2026, 9, 19)),
            (30, date(2026, 8, 27)),
            (90, date(2026, 6, 28)),
        ]:
            with self.subTest(days=days):
                start_date, end_date = search_console_reports.preset_range(
                    days, today
                )
                self.assertEqual((start_date, end_date),
                                 (start, date(2026, 9, 25)))
                self.assertEqual((end_date - start_date).days + 1, days)


class DashboardTimezoneViewTests(GSCTestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("tester", password="x")
        self.client.force_login(user)

    def test_preset_at_half_past_midnight_ist_includes_today(self):
        def fake_collect(start_date, end_date, **kwargs):
            collect_search_console_range(
                start_date, end_date, service=FakeGSCService(), **kwargs
            )

        # 19:00 UTC on 24 Sep = 00:30 IST on 25 Sep (still 24 Sep in UTC
        # and in California).
        patcher = at_utc(2026, 9, 24, 19, 0)
        self.addCleanup(patcher.stop)

        with mock.patch(
            "analytics_engine.views.collect_search_console_range",
            side_effect=fake_collect,
        ):
            response = self.client.get(
                reverse("dashboard:search_console"), {"days": "7"}
            )

        period = response.context["period"]

        self.assertEqual(
            (period["start_date"], period["end_date"], period["days"]),
            (date(2026, 9, 19), date(2026, 9, 25), 7),
        )
        self.assertEqual(len(response.context["daily_status"]), 7)
        self.assertEqual(
            response.context["daily_status"],
            ["final"] * 3 + ["preliminary"] * 2 + ["pending"] * 2,
        )
        self.assertEqual(response.context["daily_clicks"][-2:], [None, None])
        self.assertEqual(period["dashboard_timezone"], "Asia/Kolkata")
        self.assertEqual(period["timezone"], "America/Los_Angeles")
        self.assertContains(response, "19 Sep")
        self.assertContains(response, "25 Sep 2026")
        self.assertContains(response, "5 of 7 days")

    def test_custom_historical_range_is_not_affected(self):
        self.collect()

        patcher = at_utc(2026, 9, 24, 19, 0)
        self.addCleanup(patcher.stop)

        with mock.patch(
            "analytics_engine.views.collect_search_console_range"
        ) as fetch:
            response = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-18", "end": "2026-09-21"},
            )

        fetch.assert_not_called()
        self.assertEqual(
            (response.context["start_date"], response.context["end_date"]),
            (START, END),
        )
        self.assertEqual(response.context["summary"]["clicks"], 23)

    def test_custom_range_may_end_on_the_ist_day(self):
        patcher = at_utc(2026, 9, 24, 19, 0)
        self.addCleanup(patcher.stop)

        with mock.patch(
            "analytics_engine.views.collect_search_console_range"
        ):
            ok = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-19", "end": "2026-09-25"},
            )
            too_late = self.client.get(
                reverse("dashboard:search_console"),
                {"start": "2026-09-19", "end": "2026-09-26"},
            )

        self.assertNotContains(ok, "cannot be after today")
        self.assertContains(too_late, "cannot be after today (2026-09-25)")
