import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_provider import (  # noqa: E402
    DEEPSEEK_MODEL,
    ProviderConfigError,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnknownError,
    ProviderUsage,
)
from aaron_reader.ai_prompts import canonical_json  # noqa: E402
from aaron_reader.ai_service import (  # noqa: E402
    AIDisabledError,
    AIFeatureDisabledError,
    AIGenerationHeld,
    AIInputError,
    AIService,
    AIServiceError,
    TOKEN_ESTIMATE_PROTOCOL_MARGIN,
    conservative_token_estimate,
)
from aaron_reader.content import ContentSnapshot  # noqa: E402
from aaron_reader.database import AIBudgetExceeded, Database, utc_now  # noqa: E402
from aaron_reader.models import (  # noqa: E402
    AIConfig,
    AIBatchConfig,
    AIBudgetConfig,
    AppConfig,
    ArticleCandidate,
    SourceConfig,
)
from aaron_reader.normalize import stable_hash as article_hash  # noqa: E402


SOURCE = SourceConfig(
    slug="example",
    name="Example",
    home_url="https://example.com/blog",
    fetch_url="https://example.com/feed.xml",
    adapter="rss",
)


def candidate(slug, title="Article", summary="Publisher description"):
    url = "https://example.com/blog/%s" % slug
    return ArticleCandidate(
        source_slug="example",
        external_id=slug,
        url=url,
        title=title,
        summary=summary,
        published_at="2026-08-01T00:00:00Z",
        content_hash=article_hash(title, url, summary),
    )


def enabled_ai(**changes):
    budget = AIBudgetConfig(
        daily_max_requests=100,
        daily_max_total_tokens=500_000,
        monthly_max_requests=1000,
        monthly_max_total_tokens=5_000_000,
    )
    base = AIConfig(enabled=True, budget=budget)
    return replace(base, **changes)


class FakeProvider:
    def __init__(self, failure=None):
        self.requests = []
        self.failure = failure

    def generate(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        self.assert_safe_request(request)
        input_value = json.loads(request.input_text)
        language = input_value.get("target_language")
        if request.schema_name == "bilingual_report":
            output = {
                target: {
                    "headline": "%s report" % target,
                    "overview": "A grounded overview.",
                    "items": [
                        {
                            "article_id": int(item["article_id"]),
                            "title": str(item["title"]),
                            "summary": "Digest item.",
                        }
                        for item in input_value["articles"]
                    ],
                    "language": target,
                    "limitations": "Based only on supplied metadata.",
                }
                for target in ("en", "zh-CN")
            }
        elif request.schema_name == "article_summary_translation":
            output = {
                "summary": {
                    "summary": "A grounded short summary.",
                    "key_points": ["First point", "Second point"],
                    "language": language,
                    "basis": input_value["input_scope"],
                    "limitations": "Based only on supplied content.",
                },
                "translation": {
                    "title": "译文标题",
                    "publisher_summary": "译文简介",
                    "language": language,
                    "limitations": "",
                },
            }
        elif request.schema_name == "article_summary":
            output = {
                "summary": "A grounded short summary.",
                "key_points": ["First point", "Second point"],
                "language": language,
                "basis": input_value["input_scope"],
                "limitations": "Based only on supplied content.",
            }
        elif request.schema_name == "article_translation":
            output = {
                "title": "译文标题" if input_value.get("title") is not None else None,
                "publisher_summary": (
                    "译文简介" if input_value.get("publisher_summary") is not None else None
                ),
                "language": language,
                "limitations": "",
            }
        else:
            output = {
                "headline": "Today in AI",
                "overview": "A compact overview.",
                "items": [
                    {
                        "article_id": item["article_id"],
                        "title": item["title"],
                        "summary": "Digest item.",
                    }
                    for item in input_value["articles"]
                ],
                "language": language,
                "limitations": "",
            }
        return ProviderResponse(
            output_text=json.dumps(output, ensure_ascii=False),
            usage=ProviderUsage(
                input_tokens=120,
                cached_input_tokens=20,
                output_tokens=40,
                reasoning_tokens=0,
                total_tokens=160,
            ),
            model=request.model + "-resolved",
            request_id="req_test",
            response_id="resp_test",
        )

    def assert_safe_request(self, request):
        if not isinstance(request, ProviderRequest):
            raise AssertionError("expected ProviderRequest")
        self.last_input = json.loads(request.input_text)


class FakeFetcher:
    def __init__(self, allowed_hosts, **options):
        self.allowed_hosts = tuple(allowed_hosts)
        self.options = options

    def fetch(self, url):
        text = "Full article text with enough factual context for a summary."
        return ContentSnapshot(
            source_url=url,
            final_url=url,
            fetched_at="2026-08-01T00:00:00Z",
            status=200,
            content_type="text/html",
            charset="utf-8",
            etag='"v1"',
            last_modified="",
            title="Article",
            text=text,
            source_body_sha256="a" * 64,
            full_text_sha256="b" * 64,
            text_sha256="c" * 64,
            original_character_count=len(text),
            character_count=len(text),
            utf8_bytes=len(text.encode("utf-8")),
            truncated=False,
        )


class InvalidThenValidProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        usage = ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25)
        if len(self.requests) == 1:
            return ProviderResponse(
                output_text="{}",
                usage=usage,
                model=request.model,
                request_id="req_invalid",
            )
        return FakeProvider().generate(request)


class MissingUsageProvider(FakeProvider):
    def generate(self, request):
        response = super().generate(request)
        return replace(
            response,
            usage=ProviderUsage(),
            usage_reported=False,
        )


class InvalidMissingUsageProvider(MissingUsageProvider):
    def generate(self, request):
        return replace(super().generate(request), output_text="{}")


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "reader.sqlite3")
        self.database.initialize()
        self.database.sync_source_configs([SOURCE])
        self.database.commit_candidates(
            SOURCE,
            [candidate("one")],
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="one",
        )
        self.article = self.database.list_articles()[0]
        self.key = mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"})
        self.key.start()

    def tearDown(self):
        self.key.stop()
        self.temporary.cleanup()

    def app(self, ai=None):
        return AppConfig(
            sources=[SOURCE],
            ai=ai or enabled_ai(),
        )

    def test_token_reservation_uses_utf8_byte_upper_bound_and_margin(self):
        text = "𐍈" * 10_000
        estimate = conservative_token_estimate(text)
        self.assertEqual(
            len(text.encode("utf-8")) + TOKEN_ESTIMATE_PROTOCOL_MARGIN,
            estimate,
        )

    def test_disabled_mode_and_preview_never_call_provider(self):
        provider = FakeProvider()
        service = AIService(self.app(AIConfig(enabled=False)), self.database, provider=provider)
        preview = service.preview_article(
            int(self.article["id"]),
            task_type="summary",
            target_language="zh-CN",
        )
        self.assertFalse(preview["provider_will_be_called"])
        self.assertFalse(preview["ai_enabled"])
        self.assertEqual([], provider.requests)
        with self.assertRaises(AIDisabledError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual([], provider.requests)

    def test_summary_is_cached_and_never_overwrites_publisher_content(self):
        provider = FakeProvider()
        service = AIService(self.app(), self.database, provider=provider)
        first = service.generate_article(int(self.article["id"]), task_type="summary")
        second = service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(1, len(provider.requests))
        self.assertEqual("zh-CN", first["output"]["language"])
        stored_article = self.database.article(int(self.article["id"]))
        self.assertEqual("Publisher description", stored_article["summary"])
        audit = self.database.list_ai_attempts()
        self.assertEqual(1, len(audit))
        self.assertEqual(160, audit[0]["actual_total_tokens"])
        self.assertEqual("succeeded", audit[0]["state"])

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE articles SET content_hash=? WHERE id=?",
                ("publisher-metadata-version-2", int(self.article["id"])),
            )
        refreshed = service.generate_article(
            int(self.article["id"]), task_type="summary"
        )
        self.assertEqual(2, len(provider.requests))
        self.assertNotEqual(first["artifact_key"], refreshed["artifact_key"])

    def test_combined_article_call_stores_two_artifacts_and_reuses_any_provider(self):
        provider = FakeProvider()
        ai = enabled_ai(
            provider="deepseek",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            digest_model=DEEPSEEK_MODEL,
            reasoning_effort="none",
            api_key_environment="DEEPSEEK_API_KEY",
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            first = service.generate_article_pair(int(self.article["id"]))
            second = service.generate_article_pair(int(self.article["id"]))

        self.assertEqual(1, len(provider.requests))
        self.assertEqual("article_summary_translation", provider.requests[0].schema_name)
        combined_input = json.loads(provider.requests[0].input_text)
        self.assertEqual(
            {"input_scope", "target_language", "article"},
            set(combined_input),
        )
        self.assertEqual(
            1, provider.requests[0].input_text.count("Publisher description")
        )
        self.assertNotIn("summary_input", combined_input)
        self.assertNotIn("translation_input", combined_input)
        self.assertEqual(1, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual("summary", first["summary"]["task_type"])
        self.assertEqual("translation", first["translation"]["task_type"])
        latest = self.database.latest_ai_artifacts([int(self.article["id"])])
        self.assertEqual(2, len(latest[int(self.article["id"])]))
        attempts = self.database.list_ai_attempts()
        self.assertEqual(1, len(attempts))
        self.assertEqual(160, attempts[0]["actual_total_tokens"])

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_artifacts SET provider='historical-provider', "
                "requested_model='historical-model', resolved_model='historical-model'"
            )
        fresh_provider = FakeProvider()
        fresh_service = AIService(
            self.app(ai), self.database, provider=fresh_provider
        )
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            reused = fresh_service.generate_article_pair(int(self.article["id"]))
        self.assertTrue(reused["cache_hit"])
        self.assertEqual(0, reused["provider_api_calls"])
        self.assertEqual([], fresh_provider.requests)

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE articles SET content_hash=? WHERE id=?",
                ("new-publisher-content-version", int(self.article["id"])),
            )
        changed_provider = FakeProvider()
        changed_service = AIService(
            self.app(ai), self.database, provider=changed_provider
        )
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            refreshed = changed_service.generate_article_pair(
                int(self.article["id"])
            )
        self.assertEqual(1, refreshed["provider_api_calls"])
        self.assertEqual(1, len(changed_provider.requests))

    def test_bilingual_digest_uses_one_shared_input_call_and_two_cached_artifacts(self):
        provider = FakeProvider()
        ai = enabled_ai(
            provider="deepseek",
            digest_model=DEEPSEEK_MODEL,
            reasoning_effort="none",
            api_key_environment="DEEPSEEK_API_KEY",
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        articles = self.database.list_articles(limit=10)
        report_context = {
            "period": "daily",
            "timezone": "America/Los_Angeles",
            "period_start_local_date": "2026-08-01",
        }
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            first = service.generate_digest_pair(
                articles,
                report_context=report_context,
            )
            second = service.generate_digest_pair(
                articles,
                report_context=report_context,
            )

        self.assertEqual(1, len(provider.requests))
        request = provider.requests[0]
        self.assertEqual("bilingual_report", request.schema_name)
        shared_input = json.loads(request.input_text)
        self.assertEqual(["en", "zh-CN"], shared_input["target_languages"])
        self.assertNotIn("target_language", shared_input)
        self.assertEqual(1, len(shared_input["articles"]))
        self.assertEqual({"en", "zh-CN"}, set(first["artifacts"]))
        self.assertEqual(1, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(1, len(self.database.list_ai_attempts()))

    def test_bilingual_digest_partial_cache_generates_only_missing_language(self):
        provider = FakeProvider()
        ai = enabled_ai(
            provider="deepseek",
            digest_model=DEEPSEEK_MODEL,
            reasoning_effort="none",
            api_key_environment="DEEPSEEK_API_KEY",
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        articles = self.database.list_articles(limit=10)
        report_context = {
            "period": "daily",
            "timezone": "America/Los_Angeles",
            "period_start_local_date": "2026-08-01",
        }
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            service.generate_digest(
                articles,
                target_language="en",
                report_context=report_context,
            )
            result = service.generate_digest_pair(
                articles,
                report_context=report_context,
            )

        self.assertEqual(2, len(provider.requests))
        self.assertTrue(
            all(request.schema_name == "article_digest" for request in provider.requests)
        )
        self.assertEqual(1, result["provider_api_calls"])
        self.assertEqual({"en", "zh-CN"}, set(result["artifacts"]))

    def test_bilingual_digest_invalid_language_is_atomic_and_durably_held(self):
        class InvalidChineseProvider(FakeProvider):
            def generate(self, request):
                response = super().generate(request)
                output = json.loads(response.output_text)
                output["zh-CN"]["language"] = "en"
                return replace(
                    response,
                    output_text=json.dumps(output, ensure_ascii=False),
                )

        provider = InvalidChineseProvider()
        ai = enabled_ai(
            provider="deepseek",
            digest_model=DEEPSEEK_MODEL,
            reasoning_effort="none",
            api_key_environment="DEEPSEEK_API_KEY",
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        articles = self.database.list_articles(limit=10)
        report_context = {
            "period": "daily",
            "timezone": "America/Los_Angeles",
            "period_start_local_date": "2026-08-01",
        }
        prepared = {
            language: service.prepare_digest(
                articles,
                target_language=language,
                report_context=report_context,
            )
            for language in ("en", "zh-CN")
        }
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            with self.assertRaisesRegex(AIServiceError, "bilingual output"):
                service.generate_digest_pair(
                    articles,
                    report_context=report_context,
                )
            with self.assertRaises(AIGenerationHeld):
                service.generate_digest_pair(
                    articles,
                    report_context=report_context,
                )

        self.assertEqual(1, len(provider.requests))
        self.assertTrue(
            all(
                self.database.ai_artifact_by_key(task.artifact_key) is None
                for task in prepared.values()
            )
        )
        holds = self.database.list_ai_generation_holds()
        self.assertEqual(1, len(holds))
        self.assertEqual("report", holds[0]["workload_kind"])
        self.assertEqual("paid_failure", holds[0]["hold_class"])

    def test_sunday_daily_and_thirteen_article_weekly_pairs_fit_30k_reservation(self):
        candidates = []
        for index in range(12):
            slug = "weekly-%02d" % index
            url = "https://example.com/blog/%s" % slug
            title = "Weekly AI update %02d" % index
            summary = "A concise publisher description for update %02d." % index
            candidates.append(
                ArticleCandidate(
                    source_slug="example",
                    external_id=slug,
                    url=url,
                    title=title,
                    summary=summary,
                    published_at="2026-07-%02dT12:00:00Z" % (27 + index % 5),
                    content_hash=article_hash(title, url, summary),
                )
            )
        self.database.commit_candidates(
            SOURCE,
            candidates,
            started_at="2026-08-02T20:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="weekly-budget-fixture",
        )
        provider = FakeProvider()
        ai = enabled_ai(
            provider="deepseek",
            digest_model=DEEPSEEK_MODEL,
            reasoning_effort="none",
            api_key_environment="DEEPSEEK_API_KEY",
            budget=AIBudgetConfig(
                daily_max_requests=20,
                daily_max_total_tokens=30_000,
                monthly_max_requests=300,
                monthly_max_total_tokens=400_000,
            ),
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        weekly_articles = self.database.list_articles(limit=20)
        self.assertEqual(13, len(weekly_articles))
        daily_articles = [
            article
            for article in weekly_articles
            if int(article["id"]) == int(self.article["id"])
        ]
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            service.generate_digest_pair(
                daily_articles,
                report_context={
                    "period": "daily",
                    "timezone": "America/Los_Angeles",
                    "period_start_local_date": "2026-08-02",
                },
            )
            service.generate_digest_pair(
                weekly_articles,
                report_context={
                    "period": "weekly",
                    "timezone": "America/Los_Angeles",
                    "period_start_local_date": "2026-07-27",
                },
            )

        self.assertEqual(2, len(provider.requests))
        reservations = [
            conservative_token_estimate(
                request.instructions,
                request.input_text,
                canonical_json(request.json_schema),
            )
            + request.max_output_tokens
            for request in provider.requests
        ]
        self.assertLessEqual(sum(reservations), 30_000)

    def test_translation_fields_have_an_independent_cache_key(self):
        provider = FakeProvider()
        service = AIService(self.app(), self.database, provider=provider)
        title = service.generate_article(
            int(self.article["id"]),
            task_type="translation",
            translated_fields=("title",),
        )
        both = service.generate_article(
            int(self.article["id"]),
            task_type="translation",
            translated_fields=("title", "publisher_summary"),
        )
        self.assertEqual(2, len(provider.requests))
        self.assertIsNone(title["output"]["publisher_summary"])
        self.assertEqual("译文简介", both["output"]["publisher_summary"])
        self.assertNotEqual(title["artifact_key"], both["artifact_key"])

    def test_budget_is_reserved_before_any_provider_call(self):
        provider = FakeProvider()
        tiny_budget = AIBudgetConfig(
            daily_max_requests=1,
            daily_max_total_tokens=1,
            monthly_max_requests=1,
            monthly_max_total_tokens=1,
        )
        service = AIService(
            self.app(enabled_ai(budget=tiny_budget)), self.database, provider=provider
        )
        with self.assertRaises(AIBudgetExceeded):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual([], provider.requests)
        job = self.database.ai_job_for_artifact(
            service.prepare_article(int(self.article["id"]), task_type="summary").artifact_key
        )
        self.assertEqual("permanent_failed", job["state"])
        self.assertEqual("budget_impossible", job["last_error_code"])

    def test_unknown_result_is_not_automatically_retried(self):
        provider = FakeProvider(
            ProviderUnknownError("transport failed; request may have been processed")
        )
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        with self.assertRaises(AIGenerationHeld):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(self.database.list_ai_generation_holds()))
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("unknown", audit["state"])
        self.assertEqual("provider_unknown", audit["error_class"])
        self.assertEqual(1, audit["reservation_active"])

    def test_provider_config_failure_is_terminal_and_releases_reservation(self):
        provider = FakeProvider(ProviderConfigError("invalid local provider request"))
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("failed", audit["state"])
        self.assertEqual("provider_config", audit["error_class"])
        self.assertEqual(0, audit["reservation_active"])
        job = self.database.ai_job(int(audit["job_id"]))
        self.assertEqual("permanent_failed", job["state"])

    def test_known_billed_failure_records_actual_usage_without_retry(self):
        provider = FakeProvider(
            ProviderKnownError(
                "provider returned a known non-completed result",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=12,
                    output_tokens=3,
                    total_tokens=15,
                ),
                model="resolved-model",
                request_id="req_known",
                response_id="resp_known",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual("provider_known", attempt["error_class"])
        self.assertEqual("refusal", attempt["error_code"])
        self.assertEqual(15, attempt["actual_total_tokens"])
        self.assertEqual(0, attempt["reservation_active"])
        self.assertEqual("req_known", attempt["provider_request_id"])
        self.assertEqual("resolved-model", attempt["resolved_model"])
        self.assertEqual("permanent_failed", attempt["job_state"])

    def test_success_without_usage_keeps_the_conservative_reservation(self):
        provider = MissingUsageProvider()
        service = AIService(self.app(), self.database, provider=provider)
        result = service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual("succeeded", result["status"])
        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual("succeeded", attempt["state"])
        self.assertIsNone(attempt["actual_total_tokens"])
        self.assertEqual(1, attempt["reservation_active"])
        self.assertEqual("completed_usage_unreported", attempt["finish_reason"])
        status = service.status()
        self.assertEqual(int(attempt["reserved_total_tokens"]), status["daily"]["total_tokens"])

    def test_invalid_output_without_usage_is_never_automatically_retried(self):
        provider = InvalidMissingUsageProvider()
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        job = self.database.ai_job_for_artifact(
            service.prepare_article(
                int(self.article["id"]), task_type="summary"
            ).artifact_key
        )
        self.assertEqual("permanent_failed", job["state"])
        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual(1, attempt["reservation_active"])
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))

    def test_rate_limit_is_not_automatically_replayed_and_explicit_retry_is_fresh(self):
        provider = FakeProvider(
            ProviderHTTPError(
                "rate limited", status=429, retryable=True, retry_after=7_200
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        job = self.database.ai_job_for_artifact(
            service.prepare_article(int(self.article["id"]), task_type="summary").artifact_key
        )
        self.assertEqual("permanent_failed", job["state"])
        self.assertIsNone(job["next_attempt_at"])
        provider.failure = None
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
        result = service.retry_job(int(job["id"]))
        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, len(provider.requests))
        self.assertNotEqual(
            provider.requests[0].idempotency_key,
            provider.requests[1].idempotency_key,
        )
        attempts = self.database.list_ai_attempts()
        self.assertEqual(2, len(attempts))
        self.assertNotEqual(attempts[0]["idempotency_key"], attempts[1]["idempotency_key"])

    def test_ambiguous_server_error_is_unknown_and_never_auto_retried(self):
        provider = FakeProvider(
            ProviderHTTPError("temporary provider failure", status=503, retryable=True)
        )
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        with self.assertRaises(AIGenerationHeld):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(self.database.list_ai_generation_holds()))
        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual("unknown", attempt["state"])
        self.assertEqual("provider_http_unknown", attempt["error_class"])
        self.assertEqual(1, attempt["reservation_active"])

    def test_invalid_output_retry_is_a_new_billable_generation(self):
        now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
        provider = InvalidThenValidProvider()
        service = AIService(
            self.app(), self.database, provider=provider, clock=lambda: now[0]
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        now[0] += timedelta(seconds=5)
        with self.assertRaises(AIGenerationHeld):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
        result = service.generate_article(
            int(self.article["id"]),
            task_type="summary",
            force_held=True,
        )
        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, len(provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())
        self.assertNotEqual(
            provider.requests[0].idempotency_key,
            provider.requests[1].idempotency_key,
        )

    def test_full_text_fetch_is_separate_cached_and_provenanced(self):
        provider = FakeProvider()
        ai = enabled_ai(
            full_text_enabled=True,
            input_policy="fetch_on_demand_cached_local",
        )
        service = AIService(
            self.app(ai),
            self.database,
            provider=provider,
            content_fetcher_factory=FakeFetcher,
        )
        result = service.generate_article(
            int(self.article["id"]),
            task_type="summary",
            input_scope="full_text",
        )
        self.assertEqual("full_text", result["input_scope"])
        self.assertIsNotNone(result["content_snapshot_id"])
        self.assertIn("extracted_text", provider.last_input)
        cached_snapshot = service.fetch_content(int(self.article["id"]))
        self.assertTrue(cached_snapshot["cache_hit"])

        new_url = "https://example.com/blog/one-moved"
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE articles SET canonical_url=?, content_hash=? WHERE id=?",
                (new_url, "moved-content-hash", int(self.article["id"])),
            )
        with self.assertRaisesRegex(AIInputError, "no extracted full-text snapshot"):
            service.prepare_article(
                int(self.article["id"]),
                task_type="summary",
                input_scope="full_text",
                fetch_if_missing=False,
            )
        moved = service.generate_article(
            int(self.article["id"]),
            task_type="summary",
            input_scope="full_text",
        )
        self.assertEqual(new_url, provider.last_input["url"])
        self.assertNotEqual(result["artifact_key"], moved["artifact_key"])

    def test_ephemeral_full_text_runs_immediately_without_persisting_text(self):
        provider = FakeProvider()
        ai = enabled_ai(
            full_text_enabled=True,
            input_policy="fetch_on_demand_ephemeral",
            batch=AIBatchConfig(enabled=True, max_articles_per_run=2),
        )
        service = AIService(
            self.app(ai),
            self.database,
            provider=provider,
            content_fetcher_factory=FakeFetcher,
        )
        result = service.generate_article(
            int(self.article["id"]),
            task_type="summary",
            input_scope="full_text",
        )
        self.assertEqual("succeeded", result["status"])
        self.assertIsNone(result["content_snapshot_id"])
        self.assertIsNone(
            self.database.latest_content_snapshot(int(self.article["id"]))
        )
        with self.assertRaisesRegex(AIFeatureDisabledError, "cannot be queued"):
            service.enqueue_batch(
                [self.article],
                task_type="summary",
                input_scope="full_text",
                confirmed=True,
            )

    def test_explicit_retry_refetches_ephemeral_full_text(self):
        provider = FakeProvider(
            ProviderHTTPError("rate limited", status=429, retryable=True)
        )
        ai = enabled_ai(
            full_text_enabled=True,
            input_policy="fetch_on_demand_ephemeral",
        )
        service = AIService(
            self.app(ai),
            self.database,
            provider=provider,
            content_fetcher_factory=FakeFetcher,
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(
                int(self.article["id"]),
                task_type="summary",
                input_scope="full_text",
            )
        job = self.database.ai_job_for_artifact(
            service.prepare_article(
                int(self.article["id"]),
                task_type="summary",
                input_scope="full_text",
                fetch_if_missing=True,
            ).artifact_key
        )
        self.assertEqual("permanent_failed", job["state"])
        provider.failure = None
        retried = service.retry_job(int(job["id"]))
        self.assertEqual("succeeded", retried["status"])
        self.assertEqual(2, len(provider.requests))
        self.assertIsNone(
            self.database.latest_content_snapshot(int(self.article["id"]))
        )

    def test_digest_validates_exact_article_ids(self):
        self.database.commit_candidates(
            SOURCE,
            [candidate("one"), candidate("two", "Second")],
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="two",
        )
        articles = self.database.list_articles()
        provider = FakeProvider()
        service = AIService(self.app(), self.database, provider=provider)
        result = service.generate_digest(articles)
        self.assertEqual(
            [int(article["id"]) for article in articles],
            [item["article_id"] for item in result["output"]["items"]],
        )

    def test_batch_enqueue_is_confirmed_and_worker_is_bounded(self):
        self.database.commit_candidates(
            SOURCE,
            [candidate("one"), candidate("two", "Second")],
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="two",
        )
        articles = self.database.list_articles()
        provider = FakeProvider()
        ai = enabled_ai(batch=AIBatchConfig(enabled=True, max_articles_per_run=2))
        service = AIService(self.app(ai), self.database, provider=provider)
        jobs = service.enqueue_batch(
            articles,
            task_type="summary",
            confirmed=True,
        )
        self.assertEqual(2, len(jobs))
        self.assertEqual([], provider.requests)
        results = service.run_worker(limit=2)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result["state"] == "succeeded" for result in results))
        self.assertEqual(2, len(provider.requests))

    def test_worker_cancels_disabled_task_and_continues_to_the_next_job(self):
        provider = FakeProvider()
        enqueue_service = AIService(self.app(), self.database, provider=provider)
        summary = enqueue_service.enqueue(
            enqueue_service.prepare_article(
                int(self.article["id"]), task_type="summary"
            ),
            priority=1,
            trigger_kind="batch",
        )
        translation = enqueue_service.enqueue(
            enqueue_service.prepare_article(
                int(self.article["id"]), task_type="translation"
            ),
            priority=2,
            trigger_kind="batch",
        )
        worker = AIService(
            self.app(enabled_ai(summary_enabled=False)),
            self.database,
            provider=provider,
        )

        results = worker.run_worker(limit=2)

        self.assertEqual(["cancelled", "succeeded"], [item["state"] for item in results])
        self.assertEqual("cancelled", self.database.ai_job(int(summary["id"]))["state"])
        self.assertEqual(
            "succeeded", self.database.ai_job(int(translation["id"]))["state"]
        )
        self.assertEqual(1, len(provider.requests))

if __name__ == "__main__":
    unittest.main()
