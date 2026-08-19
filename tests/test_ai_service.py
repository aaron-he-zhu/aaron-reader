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
    OPENROUTER_MODEL,
    ProviderConfigError,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnknownError,
    ProviderUsage,
)
from aaron_reader.ai_prompts import parse_and_validate_output  # noqa: E402
from aaron_reader.ai_service import (  # noqa: E402
    AIDisabledError,
    AIFallbackEligibleError,
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
        if request.schema_name == "article_summary_translation":
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
            raise AssertionError("unexpected schema: %s" % request.schema_name)
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


class NullPublisherSummaryProvider(FakeProvider):
    """Model fixture that chooses the nullable schema branch for a summary."""

    def generate(self, request):
        response = super().generate(request)
        output = json.loads(response.output_text)
        if request.schema_name == "article_translation":
            output["publisher_summary"] = None
        elif request.schema_name == "article_summary_translation":
            output["translation"]["publisher_summary"] = None
        return replace(
            response,
            output_text=json.dumps(output, ensure_ascii=False),
        )


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
        self.key = mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
        )
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

    def test_service_constructs_openrouter_provider_for_fixed_profile(self):
        ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
            timeout_seconds=17,
            max_response_bytes=123_456,
        )
        service = AIService(self.app(ai), self.database)
        sentinel = object()
        with mock.patch(
            "aaron_reader.ai_service.OpenRouterChatCompletionsProvider",
            return_value=sentinel,
        ) as provider_class, mock.patch(
            "aaron_reader.ai_service.DeepSeekChatCompletionsProvider",
            side_effect=AssertionError("DeepSeek must not be constructed"),
        ) as deepseek_class:
            self.assertIs(sentinel, service._provider())
            self.assertIs(sentinel, service._provider())

        provider_class.assert_called_once_with(
            timeout_seconds=17,
            max_response_bytes=123_456,
        )
        deepseek_class.assert_not_called()

    def test_provider_switch_cannot_bypass_a_semantically_equivalent_hold(self):
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek_service = AIService(
            self.app(deepseek_ai), self.database, provider=FakeProvider()
        )
        deepseek_prepared = deepseek_service.prepare_article(
            int(self.article["id"]),
            task_type="summary",
        )
        deepseek_hold = deepseek_service._generation_hold_template(
            deepseek_prepared,
            workload_kind="article",
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **deepseek_service._classified_generation_hold(
                        deepseek_hold,
                        "ambiguous",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )

        openrouter_ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        openrouter_service = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=FakeProvider(),
        )
        openrouter_prepared = openrouter_service.prepare_article(
            int(self.article["id"]),
            task_type="summary",
        )
        openrouter_hold = openrouter_service._generation_hold_template(
            openrouter_prepared,
            workload_kind="article",
        )
        self.assertNotEqual(deepseek_hold["hold_key"], openrouter_hold["hold_key"])
        with self.assertRaises(AIGenerationHeld):
            openrouter_service._check_generation_hold(
                openrouter_hold,
                force_held=False,
            )

        observed = openrouter_service._check_generation_hold(
            openrouter_hold,
            force_held=True,
        )
        self.assertEqual(
            (),
            openrouter_service._generation_holds_cleared_after_success(
                openrouter_prepared,
                observed,
            ),
        )
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(str(deepseek_hold["hold_key"]))[
                "hold_class"
            ],
        )

        held_at = utc_now()
        existing_ambiguous = self.database.ai_generation_hold(
            str(deepseek_hold["hold_key"])
        )
        self.database.replace_ai_generation_holds(
            [
                existing_ambiguous,
                {
                    **openrouter_service._classified_generation_hold(
                        openrouter_hold,
                        "paid_failure",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                },
            ]
        )
        with self.assertRaises(AIGenerationHeld):
            openrouter_service._check_generation_hold(
                openrouter_hold,
                force_held=False,
                retry_paid_failure=True,
            )

    def test_success_cleanup_preserves_equivalent_hold_created_after_preflight(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        openrouter_service = AIService(
            self.app(openrouter_ai), self.database, provider=FakeProvider()
        )
        openrouter_prepared = openrouter_service.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        openrouter_hold = openrouter_service._generation_hold_template(
            openrouter_prepared, workload_kind="article"
        )
        observed = openrouter_service._check_generation_hold(
            openrouter_hold,
            force_held=False,
        )
        self.assertEqual((), observed)

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek_service = AIService(
            self.app(deepseek_ai), self.database, provider=FakeProvider()
        )
        deepseek_prepared = deepseek_service.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        deepseek_hold = deepseek_service._generation_hold_template(
            deepseek_prepared, workload_kind="article"
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **deepseek_service._classified_generation_hold(
                        deepseek_hold,
                        "ambiguous",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )

        openrouter_service._clear_observed_generation_holds(observed)
        self.assertIsNotNone(
            self.database.ai_generation_hold(str(deepseek_hold["hold_key"]))
        )

    def test_success_cleanup_preserves_observed_hold_if_risk_class_changed(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        primary = AIService(
            self.app(openrouter_ai), self.database, provider=FakeProvider()
        )
        prepared = primary.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        hold = primary._generation_hold_template(
            prepared, workload_kind="article"
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **primary._classified_generation_hold(
                        hold,
                        "fallback_pending",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=FakeProvider(),
            allow_fallback_pending_from="openrouter",
        )
        fallback_prepared = continuation.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        fallback_hold = continuation._generation_hold_template(
            fallback_prepared, workload_kind="article"
        )
        observed = continuation._check_generation_hold(
            fallback_hold,
            force_held=False,
        )
        self.assertEqual(1, len(observed))
        self.assertEqual(str(hold["hold_key"]), observed[0].hold_key)
        self.assertEqual("fallback_pending", observed[0].hold_class)
        self.assertFalse(observed[0].legacy_pair)

        self.database.replace_ai_generation_holds(
            [
                {
                    **primary._classified_generation_hold(hold, "ambiguous"),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )
        continuation._clear_observed_generation_holds(observed)
        remaining = self.database.ai_generation_hold(str(hold["hold_key"]))
        self.assertIsNotNone(remaining)
        self.assertEqual("ambiguous", remaining["hold_class"])

    def test_success_cleanup_only_removes_fallback_pending_holds(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        primary = AIService(
            self.app(openrouter_ai), self.database, provider=FakeProvider()
        )
        primary_prepared = primary.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        primary_hold = primary._generation_hold_template(
            primary_prepared, workload_kind="article"
        )

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        continuation = AIService(
            self.app(deepseek_ai), self.database, provider=FakeProvider()
        )
        fallback_prepared = continuation.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        fallback_hold = continuation._generation_hold_template(
            fallback_prepared, workload_kind="article"
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **primary._classified_generation_hold(
                        primary_hold,
                        "fallback_pending",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                },
                {
                    **continuation._classified_generation_hold(
                        fallback_hold,
                        "ambiguous",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                },
            ]
        )

        observed = continuation._check_generation_hold(
            fallback_hold,
            force_held=True,
        )
        continuation._clear_observed_generation_holds(observed)
        self.assertIsNone(
            self.database.ai_generation_hold(str(primary_hold["hold_key"]))
        )
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(str(fallback_hold["hold_key"]))[
                "hold_class"
            ],
        )

    def test_forced_primary_replay_cannot_downgrade_hold_for_fallback(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        openrouter_provider = FakeProvider(
            ProviderHTTPError("rate limited", status=429, retryable=True)
        )
        primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=openrouter_provider,
            automatic_fallback_provider="deepseek",
        )
        prepared = primary.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        template = primary._generation_hold_template(
            prepared, workload_kind="article"
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **primary._classified_generation_hold(
                        template, "ambiguous"
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )

        with self.assertRaises(AIFallbackEligibleError):
            primary.generate_article(
                int(self.article["id"]),
                task_type="summary",
                force_held=True,
            )
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(template["hold_key"])[
                "hold_class"
            ],
        )

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek_provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=deepseek_provider,
            allow_fallback_pending_from="openrouter",
        )
        with self.assertRaises(AIGenerationHeld):
            continuation.generate_article(
                int(self.article["id"]), task_type="summary"
            )
        self.assertEqual([], deepseek_provider.requests)

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

    def test_openrouter_article_pair_records_routed_model(self):
        routed_model = "test-provider/routed-free-model:free"

        class RoutedFakeProvider(FakeProvider):
            def generate(self, request):
                return replace(super().generate(request), model=routed_model)

        provider = RoutedFakeProvider()
        ai = enabled_ai(
            provider="openrouter",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        service = AIService(self.app(ai), self.database, provider=provider)
        with mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=True
        ):
            article_pair = service.generate_article_pair(int(self.article["id"]))

        self.assertEqual(
            ["article_summary_translation"],
            [request.schema_name for request in provider.requests],
        )
        self.assertTrue(
            all(request.model == OPENROUTER_MODEL for request in provider.requests)
        )
        self.assertEqual(routed_model, article_pair["summary"]["resolved_model"])
        self.assertEqual(routed_model, article_pair["translation"]["resolved_model"])
        self.assertEqual(
            {OPENROUTER_MODEL},
            {
                str(attempt["requested_model"])
                for attempt in self.database.list_ai_attempts()
            },
        )
        self.assertEqual(
            {routed_model},
            {
                str(attempt["resolved_model"])
                for attempt in self.database.list_ai_attempts()
            },
        )

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

    def test_empty_publisher_summary_null_is_losslessly_normalized(self):
        output = json.dumps(
            {
                "title": "译文标题",
                "publisher_summary": None,
                "language": "zh-CN",
                "limitations": "",
            },
            ensure_ascii=False,
        )
        validated, readable = parse_and_validate_output(
            "translation",
            output,
            target_language="zh-CN",
            input_scope="metadata",
            translation_input={
                "title": "Article",
                "publisher_summary": "",
            },
        )
        self.assertEqual("", validated["publisher_summary"])
        self.assertEqual("译文标题\n\n", readable)

        with self.assertRaisesRegex(ValueError, "must be a string"):
            parse_and_validate_output(
                "translation",
                output,
                target_language="zh-CN",
                input_scope="metadata",
                translation_input={
                    "title": "Article",
                    "publisher_summary": "Non-empty source summary",
                },
            )

    def test_exact_english_title_copy_is_rejected_as_untranslated(self):
        """防呆: Exact copy of English source title should be rejected."""
        english_title = "How NVIDIA scales expertise with ChatGPT Work"
        output = json.dumps(
            {
                "title": english_title,
                "publisher_summary": "这是中文简介。",
                "language": "zh-CN",
                "limitations": "",
            },
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(ValueError, "title appears untranslated"):
            parse_and_validate_output(
                "translation",
                output,
                target_language="zh-CN",
                input_scope="metadata",
                translation_input={
                    "title": english_title,
                    "publisher_summary": "English summary",
                },
            )

    def test_latin_only_output_for_chinese_target_is_rejected(self):
        """防呆: Latin-only output when target is zh-CN and source was English."""
        output = json.dumps(
            {
                "title": "Some Different But Still English Title",
                "publisher_summary": "Another completely English summary text here.",
                "language": "zh-CN",
                "limitations": "",
            },
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(ValueError, "title appears untranslated"):
            parse_and_validate_output(
                "translation",
                output,
                target_language="zh-CN",
                input_scope="metadata",
                translation_input={
                    "title": "Original English Title Here",
                    "publisher_summary": "Original English summary.",
                },
            )

    def test_mixed_chinese_with_product_names_is_accepted(self):
        """防呆: Chinese title with English product names should pass."""
        output = json.dumps(
            {
                "title": "NVIDIA 如何通过 ChatGPT Work 扩展专业知识",
                "publisher_summary": "GPT-5.6 和 Claude 等大语言模型正在改变企业工作方式。",
                "language": "zh-CN",
                "limitations": "",
            },
            ensure_ascii=False,
        )
        validated, readable = parse_and_validate_output(
            "translation",
            output,
            target_language="zh-CN",
            input_scope="metadata",
            translation_input={
                "title": "How NVIDIA scales expertise with ChatGPT Work",
                "publisher_summary": "GPT-5.6 and Claude are transforming enterprise work.",
            },
        )
        self.assertEqual("NVIDIA 如何通过 ChatGPT Work 扩展专业知识", validated["title"])
        self.assertIn("GPT-5.6", validated["publisher_summary"])
        self.assertIn("大语言模型", validated["publisher_summary"])

    def test_already_chinese_source_is_not_rejected(self):
        """Chinese source text that stays Chinese should pass."""
        chinese_title = "人工智能发展报告"
        output = json.dumps(
            {
                "title": chinese_title,
                "publisher_summary": "本报告概述了人工智能的最新进展。",
                "language": "zh-CN",
                "limitations": "",
            },
            ensure_ascii=False,
        )
        validated, _ = parse_and_validate_output(
            "translation",
            output,
            target_language="zh-CN",
            input_scope="metadata",
            translation_input={
                "title": chinese_title,
                "publisher_summary": "本报告概述了人工智能的发展。",
            },
        )
        self.assertEqual(chinese_title, validated["title"])

    def test_cached_untranslated_artifact_triggers_regeneration(self):
        """防呆: Cached translation with untranslated text should be treated as cache miss."""

        class EnglishTitleProvider(FakeProvider):
            def generate(self, request):
                return ProviderResponse(
                    output_text=json.dumps({
                        "title": "How NVIDIA scales expertise",
                        "publisher_summary": "这是中文简介内容。",
                        "language": "zh-CN",
                        "limitations": "",
                    }),
                    usage=ProviderUsage(input_tokens=50, output_tokens=30, total_tokens=80),
                    model="test-model",
                    request_id="request-1",
                )

        class ChineseTitleProvider(FakeProvider):
            def generate(self, request):
                return ProviderResponse(
                    output_text=json.dumps({
                        "title": "NVIDIA 如何扩展专业知识",
                        "publisher_summary": "这是更好的中文简介。",
                        "language": "zh-CN",
                        "limitations": "",
                    }),
                    usage=ProviderUsage(input_tokens=50, output_tokens=30, total_tokens=80),
                    model="test-model",
                    request_id="request-2",
                )

        untranslated_candidates = [
            candidate(
                "untranslated-title",
                title="How NVIDIA scales expertise",
                summary="English summary about NVIDIA.",
            ),
        ]
        self.database.commit_candidates(
            SOURCE,
            untranslated_candidates,
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="untranslated-test",
        )
        articles = {
            str(article["external_id"]): article
            for article in self.database.list_articles(limit=20)
        }
        article_id = int(articles["untranslated-title"]["id"])

        bad_provider = EnglishTitleProvider()
        bad_service = AIService(self.app(), self.database, provider=bad_provider)

        with self.assertRaisesRegex(AIServiceError, "title appears untranslated"):
            bad_service.generate_article(
                article_id,
                task_type="translation",
                target_language="zh-CN",
            )

    def test_empty_publisher_summary_null_succeeds_for_single_and_pair_calls(self):
        empty_candidates = [
            candidate("empty-single", summary=""),
            candidate("empty-pair", summary=""),
        ]
        self.database.commit_candidates(
            SOURCE,
            empty_candidates,
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="empty-summary-articles",
        )
        articles = {
            str(article["external_id"]): article
            for article in self.database.list_articles(limit=20)
        }

        single_provider = NullPublisherSummaryProvider()
        single_service = AIService(
            self.app(),
            self.database,
            provider=single_provider,
        )
        single = single_service.generate_article(
            int(articles["empty-single"]["id"]),
            task_type="translation",
        )
        self.assertEqual("", single["output"]["publisher_summary"])
        self.assertEqual(1, len(single_provider.requests))

        pair_provider = NullPublisherSummaryProvider()
        pair_service = AIService(
            self.app(),
            self.database,
            provider=pair_provider,
        )
        pair = pair_service.generate_article_pair(
            int(articles["empty-pair"]["id"]),
        )
        self.assertEqual("", pair["translation"]["output"]["publisher_summary"])
        self.assertEqual(1, len(pair_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

        rejecting_provider = NullPublisherSummaryProvider()
        rejecting_service = AIService(
            self.app(),
            self.database,
            provider=rejecting_provider,
        )
        with self.assertRaisesRegex(
            AIServiceError,
            "publisher_summary must be a string",
        ):
            rejecting_service.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual(1, len(rejecting_provider.requests))
        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )

    def test_legacy_pair_paid_hold_allows_narrow_paid_failure_retry(self):
        deepseek_ai = enabled_ai(
            provider="deepseek",
            translation_model=DEEPSEEK_MODEL,
            summary_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
            fallback_provider="",
        )
        failed_pair = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=InvalidThenValidProvider(),
        )
        with self.assertRaises(AIServiceError):
            failed_pair.generate_article_pair(int(self.article["id"]))
        hold = self.database.list_ai_generation_holds()[0]
        self.assertEqual("article_pair", hold["workload_kind"])
        self.assertEqual("paid_failure", hold["hold_class"])

        translation_provider = FakeProvider()
        translation_service = AIService(
            self.app(),
            self.database,
            provider=translation_provider,
        )
        with self.assertRaises(AIGenerationHeld):
            translation_service.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual([], translation_provider.requests)

        title_only = translation_service.generate_article(
            int(self.article["id"]),
            task_type="translation",
            translated_fields=("title",),
            force_held=True,
        )
        self.assertEqual("译文标题", title_only["output"]["title"])
        self.assertEqual(1, len(translation_provider.requests))
        self.assertEqual(1, len(self.database.list_ai_generation_holds()))

        with self.assertRaises(AIGenerationHeld):
            translation_service.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual(1, len(translation_provider.requests))

        recovered = translation_service.generate_article(
            int(self.article["id"]),
            task_type="translation",
            retry_paid_failure=True,
        )
        self.assertEqual("译文简介", recovered["output"]["publisher_summary"])
        self.assertEqual(2, len(translation_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_legacy_fallback_pending_continues_without_replaying_openrouter(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        failed_provider = FakeProvider(
            ProviderHTTPError("rate limited", status=429, retryable=True)
        )
        failed_pair = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=failed_provider,
            automatic_fallback_provider="deepseek",
        )
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "test-key"},
            clear=True,
        ):
            with self.assertRaises(AIFallbackEligibleError):
                failed_pair.generate_article_pair(int(self.article["id"]))
        legacy_hold = self.database.list_ai_generation_holds()[0]
        self.assertEqual("article_pair", legacy_hold["workload_kind"])
        self.assertEqual("fallback_pending", legacy_hold["hold_class"])

        replay_provider = FakeProvider()
        primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=replay_provider,
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIFallbackEligibleError) as paid_retry_pending:
            primary.generate_article(
                int(self.article["id"]),
                task_type="translation",
                retry_paid_failure=True,
            )
        self.assertFalse(paid_retry_pending.exception.provider_call_made)
        with self.assertRaises(AIFallbackEligibleError) as raised:
            primary.generate_article(
                int(self.article["id"]),
                task_type="translation",
                force_held=True,
            )
        self.assertFalse(raised.exception.provider_call_made)
        self.assertEqual(legacy_hold["hold_key"], raised.exception.generation_hold_key)
        self.assertEqual([], replay_provider.requests)

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        continuation_provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=continuation_provider,
            allow_fallback_pending_from="openrouter",
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            recovered = continuation.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual("译文简介", recovered["output"]["publisher_summary"])
        self.assertEqual(1, len(continuation_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_paid_retry_continues_mixed_direct_pending_on_fallback(self):
        article_id = int(self.article["id"])
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek_failure = ProviderKnownError(
            "known unusable completion",
            code="refusal",
            usage=ProviderUsage(
                input_tokens=20,
                output_tokens=5,
                total_tokens=25,
            ),
            model=DEEPSEEK_MODEL,
            request_id="req_old_deepseek_failure",
            response_id="resp_old_deepseek_failure",
        )
        failed_deepseek = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=FakeProvider(deepseek_failure),
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            with self.assertRaises(AIServiceError):
                failed_deepseek.generate_article(
                    article_id,
                    task_type="translation",
                )

        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        openrouter_provider = FakeProvider(
            ProviderHTTPError("rate limited", status=429, retryable=True)
        )
        primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=openrouter_provider,
            automatic_fallback_provider="deepseek",
        )
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "test-key"},
            clear=True,
        ):
            with self.assertRaises(AIFallbackEligibleError) as raised:
                primary.generate_article(
                    article_id,
                    task_type="translation",
                    retry_paid_failure=True,
                )
        self.assertTrue(raised.exception.provider_call_made)
        self.assertEqual(1, len(openrouter_provider.requests))
        self.assertEqual(
            ["fallback_pending", "paid_failure"],
            sorted(
                hold["hold_class"]
                for hold in self.database.list_ai_generation_holds()
            ),
        )

        resumed_provider = FakeProvider()
        resumed_primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=resumed_provider,
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIFallbackEligibleError) as resumed:
            resumed_primary.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )
        self.assertFalse(resumed.exception.provider_call_made)
        self.assertEqual([], resumed_provider.requests)

        continuation_provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=continuation_provider,
            allow_fallback_pending_from="openrouter",
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            recovered = continuation.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )

        self.assertEqual("译文简介", recovered["output"]["publisher_summary"])
        self.assertEqual(1, len(continuation_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_direct_pending_controls_fallback_and_clears_legacy_paid_hold(self):
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        failed_pair = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=InvalidThenValidProvider(),
        )
        with self.assertRaises(AIServiceError):
            failed_pair.generate_article_pair(int(self.article["id"]))
        legacy_hold = self.database.list_ai_generation_holds()[0]

        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        primary_provider = FakeProvider()
        primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=primary_provider,
            automatic_fallback_provider="deepseek",
        )
        prepared = primary.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        direct_template = primary._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        held_at = utc_now()
        direct_hold = {
            **primary._classified_generation_hold(
                direct_template,
                "fallback_pending",
            ),
            "first_seen_at": held_at,
            "last_seen_at": held_at,
        }
        self.database.replace_ai_generation_holds([legacy_hold, direct_hold])

        with self.assertRaises(AIFallbackEligibleError) as raised:
            primary.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual(direct_hold["hold_key"], raised.exception.generation_hold_key)
        self.assertEqual([], primary_provider.requests)

        continuation_provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=continuation_provider,
            allow_fallback_pending_from="openrouter",
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            continuation.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )
        self.assertEqual(1, len(continuation_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_direct_pending_cannot_override_a_legacy_ambiguous_hold(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        failed_pair = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=FakeProvider(
                ProviderUnknownError(
                    "pair transport failed after the request was sent"
                )
            ),
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIServiceError):
            failed_pair.generate_article_pair(int(self.article["id"]))
        legacy_ambiguous = self.database.list_ai_generation_holds()[0]
        self.assertEqual("article_pair", legacy_ambiguous["workload_kind"])
        self.assertEqual("ambiguous", legacy_ambiguous["hold_class"])

        prepared = failed_pair.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        direct_template = failed_pair._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        held_at = utc_now()
        direct_pending = {
            **failed_pair._classified_generation_hold(
                direct_template,
                "fallback_pending",
            ),
            "first_seen_at": held_at,
            "last_seen_at": held_at,
        }
        self.database.replace_ai_generation_holds(
            [legacy_ambiguous, direct_pending]
        )

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=provider,
            allow_fallback_pending_from="openrouter",
        )
        with self.assertRaises(AIGenerationHeld):
            continuation.generate_article(
                int(self.article["id"]),
                task_type="translation",
                retry_paid_failure=True,
            )

        self.assertEqual([], provider.requests)
        self.assertEqual(
            ["ambiguous", "fallback_pending"],
            sorted(
                hold["hold_class"]
                for hold in self.database.list_ai_generation_holds()
            ),
        )

    def test_same_second_legacy_hold_upsert_after_preflight_is_not_cleared(self):
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        failed_pair = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=InvalidThenValidProvider(),
        )
        with self.assertRaises(AIServiceError):
            failed_pair.generate_article_pair(int(self.article["id"]))
        legacy_hold = self.database.list_ai_generation_holds()[0]
        legacy_snapshot = self.database.ai_generation_hold(
            str(legacy_hold["hold_key"]),
            include_revision=True,
        )
        self.assertIsNotNone(legacy_snapshot)

        database = self.database
        hold_key = str(legacy_hold["hold_key"])
        hold_template = {
            key: legacy_hold[key]
            for key in (
                "hold_key",
                "workload_kind",
                "hold_class",
                "descriptor",
            )
        }
        original_seen_at = str(legacy_hold["last_seen_at"])
        original_revision = int(legacy_snapshot["revision"])

        class ConcurrentHoldProvider(FakeProvider):
            def generate(self, request):
                with database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    database._upsert_ai_generation_hold(
                        connection,
                        hold_template,
                        now=original_seen_at,
                    )
                    connection.commit()
                return super().generate(request)

        provider = ConcurrentHoldProvider()
        service = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=provider,
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            service.generate_article(
                int(self.article["id"]),
                task_type="translation",
                force_held=True,
            )

        remaining = self.database.ai_generation_hold(
            hold_key,
            include_revision=True,
        )
        self.assertIsNotNone(remaining)
        self.assertEqual(original_seen_at, remaining["last_seen_at"])
        self.assertGreater(remaining["revision"], original_revision)

    def test_success_does_not_clear_a_concurrent_exact_hold_revision(self):
        service = AIService(self.app(), self.database, provider=FakeProvider())
        prepared = service.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        template = service._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        hold_template = service._classified_generation_hold(
            template,
            "ambiguous",
        )
        database = self.database

        class ConcurrentExactHoldProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.concurrent_snapshot = None

            def generate(self, request):
                with database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    database._upsert_ai_generation_hold(
                        connection,
                        hold_template,
                        now=utc_now(),
                    )
                    connection.commit()
                self.concurrent_snapshot = database.ai_generation_hold(
                    str(template["hold_key"]),
                    include_revision=True,
                )
                return super().generate(request)

        provider = ConcurrentExactHoldProvider()
        service._provider_instance = provider
        result = service.generate_article(
            int(self.article["id"]),
            task_type="translation",
        )

        self.assertEqual("succeeded", result["status"])
        self.assertEqual(
            provider.concurrent_snapshot,
            self.database.ai_generation_hold(
                str(template["hold_key"]),
                include_revision=True,
            ),
        )
        self.assertEqual("ambiguous", provider.concurrent_snapshot["hold_class"])

    def test_429_does_not_settle_a_concurrent_exact_hold_revision(self):
        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        service = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=FakeProvider(),
            automatic_fallback_provider="deepseek",
        )
        prepared = service.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        template = service._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        hold_template = service._classified_generation_hold(
            template,
            "ambiguous",
        )
        database = self.database

        class Concurrent429Provider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.concurrent_snapshot = None

            def generate(self, request):
                self.requests.append(request)
                with database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    database._upsert_ai_generation_hold(
                        connection,
                        hold_template,
                        now=utc_now(),
                    )
                    connection.commit()
                self.concurrent_snapshot = database.ai_generation_hold(
                    str(template["hold_key"]),
                    include_revision=True,
                )
                raise ProviderHTTPError(
                    "rate limited",
                    status=429,
                    retryable=True,
                )

        provider = Concurrent429Provider()
        service._provider_instance = provider
        with self.assertRaises(AIFallbackEligibleError):
            service.generate_article(
                int(self.article["id"]),
                task_type="translation",
            )

        self.assertEqual(
            provider.concurrent_snapshot,
            self.database.ai_generation_hold(
                str(template["hold_key"]),
                include_revision=True,
            ),
        )
        self.assertEqual("ambiguous", provider.concurrent_snapshot["hold_class"])

    def test_final_hold_revision_conflict_cancels_pre_send_reservation(self):
        service = AIService(self.app(), self.database, provider=FakeProvider())
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        concurrent_service = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=FakeProvider(),
        )
        concurrent_prepared = concurrent_service.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        concurrent_template = concurrent_service._generation_hold_template(
            concurrent_prepared,
            workload_kind="article",
        )
        concurrent_hold = concurrent_service._classified_generation_hold(
            concurrent_template,
            "ambiguous",
        )
        original_mark = self.database.mark_ai_attempt_sent

        def mark_after_concurrent_hold(*args, **kwargs):
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self.database._upsert_ai_generation_hold(
                    connection,
                    concurrent_hold,
                    now=utc_now(),
                )
                connection.commit()
            return original_mark(*args, **kwargs)

        with mock.patch.object(
            self.database,
            "mark_ai_attempt_sent",
            side_effect=mark_after_concurrent_hold,
        ):
            with self.assertRaises(AIGenerationHeld):
                service.generate_article(
                    int(self.article["id"]),
                    task_type="translation",
                )

        self.assertEqual([], service._provider_instance.requests)
        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual("failed", attempt["state"])
        self.assertEqual(0, attempt["reservation_active"])
        self.assertEqual("generation_hold_conflict", attempt["error_class"])
        self.assertEqual("cancelled", attempt["job_state"])
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(
                str(concurrent_template["hold_key"])
            )["hold_class"],
        )

    def test_paid_failure_retry_rechecks_concurrent_risk_upgrade(self):
        provider = FakeProvider(
            ProviderKnownError(
                "known unusable completion",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=20,
                    output_tokens=5,
                    total_tokens=25,
                ),
                model="known-failure-model",
                request_id="req_known_failure",
                response_id="resp_known_failure",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])
        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")
        paid_hold = self.database.list_ai_generation_holds()[0]
        self.assertEqual("paid_failure", paid_hold["hold_class"])
        provider.failure = None

        original_check = service._check_generation_hold
        check_count = 0

        def check_with_risk_upgrade(
            template,
            *,
            force_held,
            retry_paid_failure=False,
        ):
            nonlocal check_count
            check_count += 1
            if check_count == 2:
                self.database.replace_ai_generation_holds(
                    [{**paid_hold, "hold_class": "ambiguous"}]
                )
            return original_check(
                template,
                force_held=force_held,
                retry_paid_failure=retry_paid_failure,
            )

        with mock.patch.object(
            service,
            "_check_generation_hold",
            side_effect=check_with_risk_upgrade,
        ):
            with self.assertRaises(AIGenerationHeld):
                service.generate_article(
                    article_id,
                    task_type="translation",
                    retry_paid_failure=True,
                )

        self.assertEqual(2, check_count)
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(self.database.list_ai_attempts()))
        remaining = self.database.list_ai_generation_holds()[0]
        self.assertEqual("ambiguous", remaining["hold_class"])

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
            service.generate_article(
                int(self.article["id"]),
                task_type="summary",
                retry_paid_failure=True,
            )
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(self.database.list_ai_generation_holds()))
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("unknown", audit["state"])
        self.assertEqual("provider_unknown", audit["error_class"])
        self.assertEqual(1, audit["reservation_active"])

    def test_provider_config_failure_is_terminal_and_releases_reservation(self):
        provider = FakeProvider(ProviderConfigError("invalid local provider request"))
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(ProviderConfigError):
            service.generate_article(int(self.article["id"]), task_type="summary")
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("failed", audit["state"])
        self.assertEqual("provider_config", audit["error_class"])
        self.assertEqual(0, audit["reservation_active"])
        job = self.database.ai_job(int(audit["job_id"]))
        self.assertEqual("permanent_failed", job["state"])
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_provider_construction_error_keeps_its_global_config_type(self):
        service = AIService(self.app(), self.database, provider=object())
        with self.assertRaises(ProviderConfigError):
            service.generate_article(
                int(self.article["id"]),
                task_type="summary",
            )
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("failed", audit["state"])
        self.assertEqual("provider_config", audit["error_class"])
        self.assertEqual(0, audit["reservation_active"])
        self.assertEqual([], self.database.list_ai_generation_holds())

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
        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        with self.assertRaises(AIGenerationHeld):
            service.generate_article(
                int(self.article["id"]), task_type="summary"
            )
        self.assertEqual(1, len(provider.requests))

    def test_direct_paid_failure_retry_is_fresh_and_narrowly_requeues(self):
        provider = FakeProvider(
            ProviderKnownError(
                "known unusable completion",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=20,
                    output_tokens=5,
                    total_tokens=25,
                ),
                model="known-failure-model",
                request_id="req_known_failure",
                response_id="resp_known_failure",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])
        prepared = service.prepare_article(
            article_id,
            task_type="translation",
        )

        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")
        job = self.database.ai_job_for_artifact(prepared.artifact_key)
        self.assertEqual("permanent_failed", job["state"])
        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        first_attempt = self.database.list_ai_attempts()[0]
        provider.failure = None

        with mock.patch.object(
            self.database,
            "requeue_ai_job",
            wraps=self.database.requeue_ai_job,
        ) as requeue:
            recovered = service.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )

        requeue.assert_called_once_with(int(job["id"]), allow_unknown=False)
        self.assertEqual("译文简介", recovered["output"]["publisher_summary"])
        self.assertFalse(recovered["cache_hit"])
        self.assertEqual(2, len(provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())
        attempts = self.database.list_ai_attempts()
        self.assertEqual(2, len(attempts))
        self.assertNotEqual(
            first_attempt["idempotency_key"],
            attempts[0]["idempotency_key"],
        )

    def test_paid_failure_retry_definitive_failure_settles_back_to_paid(self):
        first_failure = ProviderKnownError(
            "first known unusable completion",
            code="refusal",
            usage=ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25),
            model="known-failure-model",
            request_id="req_known_failure_1",
            response_id="resp_known_failure_1",
        )
        provider = FakeProvider(first_failure)
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])

        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")
        first_hold = self.database.list_ai_generation_holds(
            include_revision=True
        )[0]
        self.assertEqual("paid_failure", first_hold["hold_class"])

        provider.failure = ProviderKnownError(
            "second known unusable completion",
            code="refusal",
            usage=ProviderUsage(input_tokens=22, output_tokens=6, total_tokens=28),
            model="known-failure-model",
            request_id="req_known_failure_2",
            response_id="resp_known_failure_2",
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )

        remaining = self.database.list_ai_generation_holds(
            include_revision=True
        )[0]
        self.assertEqual("paid_failure", remaining["hold_class"])
        self.assertGreater(int(remaining["revision"]), int(first_hold["revision"]))
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(2, len(self.database.list_ai_attempts()))

    def test_forced_paid_failure_replay_definitive_failure_remains_paid(self):
        failure = ProviderKnownError(
            "known unusable completion",
            code="refusal",
            usage=ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25),
            model="known-failure-model",
            request_id="req_forced_paid_1",
            response_id="resp_forced_paid_1",
        )
        provider = FakeProvider(failure)
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])
        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")

        provider.failure = ProviderKnownError(
            "known unusable completion again",
            code="refusal",
            usage=ProviderUsage(input_tokens=21, output_tokens=5, total_tokens=26),
            model="known-failure-model",
            request_id="req_forced_paid_2",
            response_id="resp_forced_paid_2",
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(
                article_id,
                task_type="translation",
                force_held=True,
            )

        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        self.assertEqual(2, len(provider.requests))

    def test_paid_failure_retry_unknown_result_remains_ambiguous(self):
        provider = FakeProvider(
            ProviderKnownError(
                "known unusable completion",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=20,
                    output_tokens=5,
                    total_tokens=25,
                ),
                model="known-failure-model",
                request_id="req_paid_before_unknown",
                response_id="resp_paid_before_unknown",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])
        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")

        provider.failure = ProviderUnknownError(
            "transport failed after the paid replay was sent"
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )

        self.assertEqual(
            "ambiguous",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        attempts = self.database.list_ai_attempts()
        self.assertEqual(2, len(attempts))
        self.assertEqual("unknown", attempts[0]["state"])
        self.assertEqual(2, len(provider.requests))

    def test_paid_failure_retry_429_without_fallback_restores_paid_hold(self):
        provider = FakeProvider(
            ProviderKnownError(
                "known unusable completion",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=20,
                    output_tokens=5,
                    total_tokens=25,
                ),
                model="known-failure-model",
                request_id="req_paid_before_429",
                response_id="resp_paid_before_429",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        article_id = int(self.article["id"])
        with self.assertRaises(AIServiceError):
            service.generate_article(article_id, task_type="translation")

        provider.failure = ProviderHTTPError(
            "rate limited",
            status=429,
            retryable=True,
        )
        with self.assertRaises(AIServiceError):
            service.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )

        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        self.assertEqual(2, len(provider.requests))

    def test_paid_retry_429_settles_pending_and_deepseek_succeeds(self):
        article_id = int(self.article["id"])
        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        openrouter_provider = FakeProvider(
            ProviderKnownError(
                "known unusable completion",
                code="refusal",
                usage=ProviderUsage(
                    input_tokens=20,
                    output_tokens=5,
                    total_tokens=25,
                ),
                model=OPENROUTER_MODEL,
                request_id="req_openrouter_paid",
                response_id="resp_openrouter_paid",
            )
        )
        primary = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=openrouter_provider,
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIServiceError):
            primary.generate_article(article_id, task_type="translation")
        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )

        openrouter_provider.failure = ProviderHTTPError(
            "rate limited",
            status=429,
            retryable=True,
        )
        with self.assertRaises(AIFallbackEligibleError) as raised:
            primary.generate_article(
                article_id,
                task_type="translation",
                retry_paid_failure=True,
            )
        self.assertTrue(raised.exception.provider_call_made)
        self.assertEqual(
            ["fallback_pending"],
            [
                hold["hold_class"]
                for hold in self.database.list_ai_generation_holds()
            ],
        )

        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek_provider = FakeProvider()
        continuation = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=deepseek_provider,
            allow_fallback_pending_from="openrouter",
        )
        recovered = continuation.generate_article(
            article_id,
            task_type="translation",
        )

        self.assertEqual("译文简介", recovered["output"]["publisher_summary"])
        self.assertEqual(2, len(openrouter_provider.requests))
        self.assertEqual(1, len(deepseek_provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_cross_provider_paid_failure_retry_clears_observed_snapshot(self):
        deepseek_ai = enabled_ai(
            provider="deepseek",
            fallback_provider="",
            summary_model=DEEPSEEK_MODEL,
            translation_model=DEEPSEEK_MODEL,
            api_key_environment="DEEPSEEK_API_KEY",
        )
        deepseek = AIService(
            self.app(deepseek_ai),
            self.database,
            provider=FakeProvider(),
        )
        prepared = deepseek.prepare_article(
            int(self.article["id"]),
            task_type="translation",
        )
        template = deepseek._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        held_at = utc_now()
        self.database.replace_ai_generation_holds(
            [
                {
                    **deepseek._classified_generation_hold(
                        template,
                        "paid_failure",
                    ),
                    "first_seen_at": held_at,
                    "last_seen_at": held_at,
                }
            ]
        )

        openrouter_ai = enabled_ai(
            provider="openrouter",
            fallback_provider="",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )
        provider = FakeProvider()
        openrouter = AIService(
            self.app(openrouter_ai),
            self.database,
            provider=provider,
        )
        recovered = openrouter.generate_article(
            int(self.article["id"]),
            task_type="translation",
            retry_paid_failure=True,
        )

        self.assertEqual("openrouter", recovered["provider"])
        self.assertEqual(1, len(provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())

    def test_known_availability_failure_without_fallback_remains_held(self):
        provider = FakeProvider(
            ProviderKnownError(
                "provider returned a final unavailable completion",
                code="provider_unavailable",
                usage=ProviderUsage(
                    input_tokens=12,
                    output_tokens=3,
                    total_tokens=15,
                ),
                model="resolved-model",
                request_id="req_unavailable",
                response_id="resp_unavailable",
            )
        )
        service = AIService(self.app(), self.database, provider=provider)
        with self.assertRaises(AIServiceError):
            service.generate_article(
                int(self.article["id"]), task_type="summary"
            )
        self.assertEqual(
            "paid_failure",
            self.database.list_ai_generation_holds()[0]["hold_class"],
        )
        with self.assertRaises(AIGenerationHeld):
            service.generate_article(
                int(self.article["id"]), task_type="summary"
            )
        self.assertEqual(1, len(provider.requests))

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

    def test_openrouter_http_fallback_matrix_is_closed_and_persisted(self):
        self.assertTrue(AIService._http_failure_is_ambiguous(503))
        self.assertFalse(AIService._http_failure_is_ambiguous(400))
        cases = (
            (401, False, True, "fallback_pending", "permanent_failed"),
            (402, False, True, "fallback_pending", "permanent_failed"),
            (404, False, True, "fallback_pending", "permanent_failed"),
            (429, True, True, "fallback_pending", "permanent_failed"),
            (400, False, False, "paid_failure", "permanent_failed"),
            (403, False, False, "paid_failure", "permanent_failed"),
            (422, False, False, "paid_failure", "permanent_failed"),
            (408, True, False, "ambiguous", "unknown"),
            (409, True, False, "ambiguous", "unknown"),
            (425, True, False, "ambiguous", "unknown"),
            (500, True, False, "ambiguous", "unknown"),
            (503, True, False, "ambiguous", "unknown"),
        )
        extra = [
            candidate("http-%d" % status, title="HTTP %d" % status)
            for status, _, _, _, _ in cases
        ]
        self.database.commit_candidates(
            SOURCE,
            extra,
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="http-matrix",
        )
        articles = {
            str(article["external_id"]): article
            for article in self.database.list_articles(limit=100)
        }
        ai = enabled_ai(
            provider="openrouter",
            fallback_provider="deepseek",
            summary_model=OPENROUTER_MODEL,
            translation_model=OPENROUTER_MODEL,
            reasoning_effort="none",
            api_key_environment="OPENROUTER_API_KEY",
        )

        for status, retryable, eligible, hold_class, job_state in cases:
            with self.subTest(status=status):
                provider = FakeProvider(
                    ProviderHTTPError(
                        "HTTP %d fixture" % status,
                        status=status,
                        retryable=retryable,
                    )
                )
                service = AIService(
                    self.app(ai),
                    self.database,
                    provider=provider,
                    automatic_fallback_provider="deepseek",
                )
                article_id = int(articles["http-%d" % status]["id"])
                prepared = service.prepare_article(
                    article_id, task_type="summary"
                )
                with self.assertRaises(AIServiceError) as raised:
                    service.generate_article(article_id, task_type="summary")
                self.assertEqual(
                    eligible,
                    isinstance(raised.exception, AIFallbackEligibleError),
                )
                hold = self.database.ai_generation_hold(
                    service._generation_hold_template(
                        prepared, workload_kind="article"
                    )["hold_key"]
                )
                self.assertIsNotNone(hold)
                self.assertEqual(hold_class, hold["hold_class"])
                job = self.database.ai_job_for_artifact(prepared.artifact_key)
                self.assertEqual(job_state, job["state"])
                self.assertEqual(1, len(provider.requests))

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
        first_key = str(attempt["idempotency_key"])

        provider.failure = None
        recovered = service.retry_job(int(attempt["job_id"]), allow_unknown=True)
        self.assertEqual("succeeded", recovered["status"])
        self.assertEqual(2, len(provider.requests))
        self.assertEqual([], self.database.list_ai_generation_holds())
        attempts = self.database.list_ai_attempts()
        self.assertEqual(2, len(attempts))
        self.assertNotEqual(first_key, str(attempts[0]["idempotency_key"]))

    def test_hard_crash_after_mark_sent_leaves_a_no_replay_hold(self):
        class FatalProvider:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise KeyboardInterrupt("simulated process termination")

        provider = FatalProvider()
        service = AIService(self.app(), self.database, provider=provider)
        prepared = service.prepare_article(
            int(self.article["id"]), task_type="summary"
        )
        with self.assertRaises(KeyboardInterrupt):
            service.generate_article(
                int(self.article["id"]), task_type="summary"
            )

        attempt = self.database.list_ai_attempts()[0]
        self.assertEqual("sent", attempt["state"])
        self.assertEqual("sent", attempt["job_state"])
        hold_key = service._generation_hold_template(
            prepared, workload_kind="article"
        )["hold_key"]
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(hold_key)["hold_class"],
        )

        replay_provider = FakeProvider()
        replay_service = AIService(
            self.app(), self.database, provider=replay_provider
        )
        with self.assertRaises(AIGenerationHeld):
            replay_service.generate_article(
                int(self.article["id"]), task_type="summary"
            )
        self.assertEqual([], replay_provider.requests)

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
