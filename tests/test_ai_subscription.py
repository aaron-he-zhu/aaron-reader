import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_prompts import stable_hash  # noqa: E402
from aaron_reader.ai_provider import (  # noqa: E402
    DEEPSEEK_MODEL,
    ProviderResponse,
    ProviderUsage,
)
from aaron_reader.ai_service import AIInputError, AIService  # noqa: E402
from aaron_reader.ai_subscription import (  # noqa: E402
    SUBSCRIPTION_REPORT_PROTOCOL,
    export_subscription_batch,
    export_subscription_report,
    generate_cloud_report_pair,
    import_subscription_report,
    import_subscription_results,
    report_period_window,
)
from aaron_reader.database import Database  # noqa: E402
from aaron_reader.models import (  # noqa: E402
    AIConfig,
    AppConfig,
    ArticleCandidate,
    SourceConfig,
)


class AISubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "reader.sqlite3")
        self.database.initialize()
        self.source = SourceConfig(
            slug="example",
            name="Example",
            home_url="https://example.com/blog",
            fetch_url="https://example.com/feed.xml",
            adapter="rss",
        )
        self.database.sync_source_configs([self.source])
        candidates = []
        for external_id, published_at in (
            ("before-week", "2026-07-26T12:00:00Z"),
            ("this-week", "2026-07-28T12:00:00Z"),
            ("today", "2026-08-01T08:00:00Z"),
        ):
            url = "https://example.com/blog/%s" % external_id
            title = "Title %s" % external_id
            summary = "Publisher summary %s" % external_id
            candidates.append(
                ArticleCandidate(
                    source_slug=self.source.slug,
                    external_id=external_id,
                    url=url,
                    title=title,
                    summary=summary,
                    published_at=published_at,
                    content_hash=stable_hash([title, url, summary]),
                )
            )
        self.database.commit_candidates(
            self.source,
            candidates,
            started_at="2026-08-01T17:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="fixture",
        )
        self.config = AppConfig(
            sources=[self.source],
            ai=AIConfig(enabled=False),
        )
        self.service = AIService(self.config, self.database)
        self.now = datetime(2026, 8, 1, 17, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def article_result(request, *, include_summary=True, include_translation=True):
        item = request["items"][0]
        return {
            "protocol": request["protocol"],
            "batch_id": request["batch_id"],
            "target_language": request["target_language"],
            "items": [
                {
                    "article_id": item["article_id"],
                    "fingerprint": item["fingerprint"],
                    "summary": (
                        {
                            "summary": "摘要",
                            "key_points": ["要点"],
                            "language": "zh-CN",
                            "basis": "metadata",
                            "limitations": "仅依据元数据",
                        }
                        if include_summary
                        else None
                    ),
                    "translation": (
                        {
                            "title": "标题译文",
                            "publisher_summary": "简介译文",
                            "language": "zh-CN",
                            "limitations": "",
                        }
                        if include_translation
                        else None
                    ),
                }
            ],
        }

    @staticmethod
    def report_result(request):
        articles = request["input"]["articles"]
        return {
            key: request[key]
            for key in (
                "protocol",
                "report_id",
                "fingerprint",
                "period",
                "timezone",
                "local_date",
                "period_start",
                "period_end",
                "target_language",
            )
        } | {
            "output": {
                "headline": "今日摘要",
                "overview": "这是严格基于发布方元数据的概览。",
                "items": [
                    {
                        "article_id": article["article_id"],
                        "title": "摘要：%s" % article["title"],
                        "summary": "该文章的简短说明。",
                    }
                    for article in articles
                ],
                "language": "zh-CN",
                "limitations": "没有读取文章全文。",
            }
        }

    def test_targeted_summary_accepts_null_translation_and_rejects_extra_output(self):
        article = self.database.list_articles(limit=1)[0]
        request = export_subscription_batch(
            self.service,
            [article],
            target_language="zh-CN",
            tasks=("summary",),
        )
        self.assertEqual(["summary"], request["items"][0]["missing_tasks"])
        self.assertEqual({"summary"}, set(request["tasks"]))

        invalid = self.article_result(
            request, include_summary=True, include_translation=True
        )
        with self.assertRaisesRegex(AIInputError, "unrequested translation"):
            import_subscription_results(self.service, invalid)
        self.assertEqual({}, self.database.latest_ai_artifacts([int(article["id"])]))

        valid = self.article_result(
            request, include_summary=True, include_translation=False
        )
        first = import_subscription_results(self.service, valid)
        second = import_subscription_results(self.service, valid)
        self.assertEqual((1, 0), (first["imported_artifacts"], first["cache_hits"]))
        self.assertEqual((0, 1), (second["imported_artifacts"], second["cache_hits"]))

    def test_targeted_translation_accepts_null_summary(self):
        article = self.database.list_articles(limit=1)[0]
        request = export_subscription_batch(
            self.service,
            [article],
            target_language="zh-CN",
            tasks=("translation",),
        )
        self.assertEqual(["translation"], request["items"][0]["missing_tasks"])
        self.assertEqual({"translation"}, set(request["tasks"]))
        result = import_subscription_results(
            self.service,
            self.article_result(
                request, include_summary=False, include_translation=True
            ),
        )
        self.assertEqual(1, result["imported_artifacts"])
        artifacts = self.database.latest_ai_artifacts([int(article["id"])])
        self.assertEqual("translation", artifacts[int(article["id"])][0]["task_type"])

    def test_translation_preserves_an_empty_requested_publisher_summary(self):
        url = "https://example.com/blog/empty-summary"
        title = "An article without a publisher summary"
        self.database.commit_candidates(
            self.source,
            [
                ArticleCandidate(
                    source_slug=self.source.slug,
                    external_id="empty-summary",
                    url=url,
                    title=title,
                    summary="",
                    published_at="2026-08-01T09:00:00Z",
                    content_hash=stable_hash([title, url, ""]),
                )
            ],
            started_at="2026-08-01T17:01:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="empty-summary-fixture",
        )
        article = next(
            item
            for item in self.database.list_articles(limit=20)
            if item["canonical_url"] == url
        )
        request = export_subscription_batch(
            self.service,
            [article],
            target_language="zh-CN",
            tasks=("translation",),
        )
        self.assertEqual("", request["items"][0]["input"]["publisher_summary"])
        payload = self.article_result(
            request,
            include_summary=False,
            include_translation=True,
        )
        payload["items"][0]["translation"]["publisher_summary"] = ""

        result = import_subscription_results(self.service, payload)

        self.assertEqual(1, result["imported_artifacts"])
        artifacts = self.database.latest_ai_artifacts([int(article["id"])])
        output = json.loads(artifacts[int(article["id"])][0]["output_json"])
        self.assertEqual("", output["publisher_summary"])

    def test_san_francisco_daily_weekly_and_dst_boundaries(self):
        daily = report_period_window("daily", now=self.now)
        weekly = report_period_window("weekly", now=self.now)
        self.assertEqual("2026-08-01", daily["local_date"])
        self.assertEqual("2026-08-01T07:00:00Z", daily["period_start"])
        self.assertEqual("2026-07-27T07:00:00Z", weekly["period_start"])
        self.assertEqual("2026-08-01T17:00:00Z", weekly["period_end"])

        fallback = report_period_window(
            "daily",
            now=datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),
        )
        self.assertEqual("2026-11-01T07:00:00Z", fallback["period_start"])
        self.assertEqual("2026-11-01T09:30:00Z", fallback["period_end"])

    def test_daily_and_weekly_exports_use_exact_calendar_article_sets(self):
        daily = export_subscription_report(
            self.service, period="daily", now=self.now
        )
        weekly = export_subscription_report(
            self.service, period="weekly", now=self.now
        )
        self.assertEqual(SUBSCRIPTION_REPORT_PROTOCOL, daily["protocol"])
        self.assertEqual(1, daily["article_count"])
        self.assertEqual(2, weekly["article_count"])
        self.assertEqual(
            ["today"],
            [article["url"].rsplit("/", 1)[-1] for article in daily["input"]["articles"]],
        )
        self.assertEqual(
            ["today", "this-week"],
            [article["url"].rsplit("/", 1)[-1] for article in weekly["input"]["articles"]],
        )
        self.assertFalse(daily["generation"]["api_key_required"])

    def test_report_import_is_atomic_idempotent_and_has_no_provider_attempt(self):
        request = export_subscription_report(
            self.service, period="daily", now=self.now
        )
        payload = self.report_result(request)
        first = import_subscription_report(self.service, payload)
        second = import_subscription_report(self.service, payload)
        self.assertEqual((1, 0), (first["imported_artifacts"], first["cache_hits"]))
        self.assertEqual((0, 1), (second["imported_artifacts"], second["cache_hits"]))
        self.assertEqual(0, len(self.database.list_ai_attempts()))
        reports = self.database.latest_ai_reports()
        self.assertEqual(1, len(reports))
        self.assertEqual("daily", reports[0]["period"])
        self.assertEqual("America/Los_Angeles", reports[0]["timezone"])

        cached = export_subscription_report(
            self.service, period="daily", now=self.now
        )
        self.assertEqual(0, cached["pending_count"])
        self.assertTrue(cached["cached"])
        self.assertIsNone(cached["input"])

    def test_cloud_report_pair_uses_one_call_and_attaches_both_reports(self):
        overflow_candidates = []
        for index in range(55):
            external_id = "daily-overflow-%02d" % index
            url = "https://example.com/blog/%s" % external_id
            title = "Daily update %02d" % index
            summary = "Short publisher note %02d." % index
            overflow_candidates.append(
                ArticleCandidate(
                    source_slug=self.source.slug,
                    external_id=external_id,
                    url=url,
                    title=title,
                    summary=summary,
                    published_at="2026-08-01T09:%02d:00Z" % (index % 60),
                    content_hash=stable_hash([title, url, summary]),
                )
            )
        self.database.commit_candidates(
            self.source,
            overflow_candidates,
            started_at="2026-08-01T17:01:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="daily-overflow-fixture",
        )

        class BilingualProvider:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                payload = json.loads(request.input_text)
                output = {
                    language: {
                        "headline": "%s headline" % language,
                        "overview": "%s overview" % language,
                        "items": [
                            {
                                "article_id": int(article["article_id"]),
                                "title": str(article["title"]),
                                "summary": "%s item" % language,
                            }
                            for article in payload["articles"]
                        ],
                        "language": language,
                        "limitations": "Metadata only.",
                    }
                    for language in ("en", "zh-CN")
                }
                return ProviderResponse(
                    output_text=json.dumps(output, ensure_ascii=False),
                    usage=ProviderUsage(
                        input_tokens=100,
                        output_tokens=80,
                        total_tokens=180,
                    ),
                    model=DEEPSEEK_MODEL,
                    request_id="bilingual-test",
                )

        provider = BilingualProvider()
        service = AIService(
            AppConfig(
                sources=[self.source],
                ai=AIConfig(enabled=True),
            ),
            self.database,
            provider=provider,
        )
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            first = generate_cloud_report_pair(
                service,
                period="daily",
                now=self.now,
            )
            second = generate_cloud_report_pair(
                service,
                period="daily",
                now=self.now,
            )

        self.assertEqual(1, len(provider.requests))
        self.assertEqual("bilingual_report", provider.requests[0].schema_name)
        request_payload = json.loads(provider.requests[0].input_text)
        self.assertEqual(50, len(request_payload["articles"]))
        self.assertEqual(1, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        self.assertEqual(
            ["en", "zh-CN"],
            [report["target_language"] for report in first["reports"]],
        )
        self.assertTrue(
            all(
                report.get("generation_mode") == "bilingual_shared"
                for report in first["reports"]
            )
        )
        reports = self.database.latest_ai_reports()
        self.assertEqual(2, len(reports))
        self.assertTrue(all(int(report["input_truncated"]) == 1 for report in reports))
        self.assertTrue(
            all(len(json.loads(str(report["article_ids_json"]))) == 50 for report in reports)
        )
        self.assertTrue(
            all(
                len(json.loads(str(report["output_json"]))["items"]) == 50
                for report in reports
            )
        )
        self.assertEqual(1, len(self.database.list_ai_attempts()))

    def test_overflow_partial_cache_legacy_import_then_generates_only_missing_language(self):
        candidates = []
        for index in range(51):
            external_id = "partial-overflow-%02d" % index
            url = "https://example.com/blog/%s" % external_id
            title = "Partial daily update %02d" % index
            summary = "Short partial-cache note %02d." % index
            candidates.append(
                ArticleCandidate(
                    source_slug=self.source.slug,
                    external_id=external_id,
                    url=url,
                    title=title,
                    summary=summary,
                    published_at="2026-08-01T10:%02d:00Z" % (index % 60),
                    content_hash=stable_hash([title, url, summary]),
                )
            )
        self.database.commit_candidates(
            self.source,
            candidates,
            started_at="2026-08-01T17:02:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="partial-overflow-fixture",
        )

        # Exercise the legacy/offline single-language attachment first.
        chinese_request = export_subscription_report(
            self.service,
            period="daily",
            target_language="zh-CN",
            now=self.now,
        )
        self.assertEqual(50, len(chinese_request["input"]["articles"]))
        imported = import_subscription_report(
            self.service,
            self.report_result(chinese_request),
        )
        self.assertEqual(1, imported["imported_artifacts"])

        class EnglishOnlyProvider:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                payload = json.loads(request.input_text)
                language = payload["target_language"]
                output = {
                    "headline": "English headline",
                    "overview": "English overview.",
                    "items": [
                        {
                            "article_id": int(article["article_id"]),
                            "title": str(article["title"]),
                            "summary": "English item.",
                        }
                        for article in payload["articles"]
                    ],
                    "language": language,
                    "limitations": "Metadata only.",
                }
                return ProviderResponse(
                    output_text=json.dumps(output),
                    usage=ProviderUsage(
                        input_tokens=90,
                        output_tokens=40,
                        total_tokens=130,
                    ),
                    model=DEEPSEEK_MODEL,
                    request_id="partial-cache-test",
                )

        provider = EnglishOnlyProvider()
        service = AIService(
            AppConfig(sources=[self.source], ai=AIConfig(enabled=True)),
            self.database,
            provider=provider,
        )
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            first = generate_cloud_report_pair(
                service,
                period="daily",
                now=self.now,
            )
            second = generate_cloud_report_pair(
                service,
                period="daily",
                now=self.now,
            )

        self.assertEqual(1, len(provider.requests))
        self.assertEqual("article_digest", provider.requests[0].schema_name)
        self.assertEqual("en", json.loads(provider.requests[0].input_text)["target_language"])
        self.assertEqual(1, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        reports = self.database.latest_ai_reports()
        self.assertEqual({"en", "zh-CN"}, {report["target_language"] for report in reports})
        self.assertTrue(
            all(len(json.loads(str(report["article_ids_json"]))) == 50 for report in reports)
        )

    def test_report_rejects_article_changes_without_partial_storage(self):
        request = export_subscription_report(
            self.service, period="daily", now=self.now
        )
        payload = self.report_result(request)
        article_id = request["input"]["articles"][0]["article_id"]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE articles SET content_hash=? WHERE id=?",
                ("f" * 64, int(article_id)),
            )
        with self.assertRaisesRegex(AIInputError, "changed after export"):
            import_subscription_report(self.service, payload)
        self.assertEqual([], self.database.latest_ai_reports())
        self.assertIsNone(
            self.database.ai_artifact_by_key(
                str(request["report_id"])
            )
        )


if __name__ == "__main__":
    unittest.main()
