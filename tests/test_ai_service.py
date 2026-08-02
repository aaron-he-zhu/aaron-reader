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
    ProviderConfigError,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnknownError,
    ProviderUsage,
)
from aaron_reader.ai_service import (  # noqa: E402
    AIDisabledError,
    AIFeatureDisabledError,
    AIInputError,
    AIService,
    AIServiceError,
    AIWebController,
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
        language = input_value["target_language"]
        if request.schema_name == "article_summary":
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
        self.key = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})
        self.key.start()

    def tearDown(self):
        self.key.stop()
        self.temporary.cleanup()

    def app(self, ai=None):
        return AppConfig(
            sources=[SOURCE],
            notification_enabled=False,
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
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
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
        with self.assertRaises(AIServiceError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertEqual(1, len(provider.requests))
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
        result = service.generate_article(int(self.article["id"]), task_type="summary")
        self.assertFalse(result["cache_hit"])
        self.assertEqual(2, len(provider.requests))
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

    def test_web_ephemeral_full_text_uses_the_in_memory_prepared_input(self):
        provider = FakeProvider()
        ai = enabled_ai(
            full_text_enabled=True,
            web_actions_enabled=True,
            input_policy="fetch_on_demand_ephemeral",
        )
        service = AIService(
            self.app(ai),
            self.database,
            provider=provider,
            content_fetcher_factory=FakeFetcher,
        )
        controller = AIWebController(service)

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with mock.patch(
            "aaron_reader.ai_service.threading.Thread", ImmediateThread
        ):
            submitted = controller.submit(
                {
                    "article_id": int(self.article["id"]),
                    "task": "summary",
                    "target_language": "zh-CN",
                    "input_scope": "full_text",
                },
                "web-ephemeral-full-text",
            )

        self.assertEqual("queued", submitted["state"])
        self.assertEqual(
            "succeeded", self.database.ai_job(int(submitted["job_id"]))["state"]
        )
        self.assertEqual(1, len(provider.requests))
        self.assertIsNone(
            self.database.latest_content_snapshot(int(self.article["id"]))
        )

    def test_web_does_not_start_a_budget_blocked_job_before_reset(self):
        provider = FakeProvider()
        service = AIService(self.app(), self.database, provider=provider)
        prepared = service.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        job = service.enqueue(prepared, priority=5, trigger_kind="web")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_jobs SET state='budget_blocked', "
                "next_attempt_at='2999-01-01T00:00:00Z' WHERE id=?",
                (int(job["id"]),),
            )

        controller = AIWebController(service)
        with mock.patch("aaron_reader.ai_service.threading.Thread") as thread_class:
            submitted = controller.submit(
                {
                    "article_id": int(self.article["id"]),
                    "task": "summary",
                    "target_language": "zh-CN",
                },
                "web-budget-wait",
            )

        thread_class.assert_not_called()
        self.assertEqual("budget_blocked", submitted["state"])
        self.assertEqual("2999-01-01T00:00:00Z", submitted["next_attempt_at"])
        self.assertEqual([], provider.requests)


if __name__ == "__main__":
    unittest.main()
