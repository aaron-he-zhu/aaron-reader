import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_provider import (  # noqa: E402
    DEEPSEEK_MODEL,
    OPENROUTER_MODEL,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderResponse,
    ProviderUnknownError,
    ProviderUsage,
)
from aaron_reader.cli import _weekly_report_due, main  # noqa: E402
from aaron_reader.database import Database, utc_now  # noqa: E402
from aaron_reader.models import ArticleCandidate, SourceConfig  # noqa: E402
from aaron_reader.normalize import stable_hash  # noqa: E402


class AICliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "reader.sqlite3"
        self.output_dir = self.root / "public"
        self.config_path = self.root / "sources.json"
        self.source = SourceConfig(
            slug="example",
            name="Example",
            home_url="https://example.com/blog",
            fetch_url="https://example.com/feed.xml",
            adapter="rss",
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "database_path": str(self.database_path),
                    "output_dir": str(self.output_dir),
                    "ai": {
                        "enabled": True,
                        "features": {
                            "summary": True,
                            "translation": True,
                            "digest": True,
                            "full_text": False,
                            "web_actions": False,
                        },
                        "budget": {
                            "daily_max_requests": 20,
                            "daily_max_total_tokens": 100000,
                            "monthly_max_requests": 100,
                            "monthly_max_total_tokens": 1000000,
                        },
                        "batch": {"enabled": True},
                    },
                    "sources": [
                        {
                            "slug": self.source.slug,
                            "name": self.source.name,
                            "home_url": self.source.home_url,
                            "fetch_url": self.source.fetch_url,
                            "adapter": self.source.adapter,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        database = Database(self.database_path)
        database.initialize()
        database.sync_source_configs([self.source])
        url = "https://example.com/blog/one"
        database.commit_candidates(
            self.source,
            [
                ArticleCandidate(
                    source_slug="example",
                    external_id="one",
                    url=url,
                    title="Article",
                    summary="Publisher description",
                    published_at="2026-08-01T00:00:00Z",
                    content_hash=stable_hash("Article", url, "Publisher description"),
                )
            ],
            started_at=utc_now(),
            http_status=200,
            etag="",
            last_modified="",
            body_hash="body",
        )
        self.article_id = int(database.list_articles()[0]["id"])

    def tearDown(self):
        self.temporary.cleanup()

    def provider_response(self):
        return ProviderResponse(
            output_text=json.dumps(
                {
                    "summary": "CLI summary",
                    "key_points": [],
                    "language": "zh-CN",
                    "basis": "metadata",
                    "limitations": "",
                }
            ),
            usage=ProviderUsage(input_tokens=50, output_tokens=20, total_tokens=70),
            model="test-resolved",
            request_id="req_cli",
        )

    def cloud_provider_response(self, request):
        input_value = json.loads(request.input_text)
        language = input_value.get("target_language")
        if request.schema_name == "bilingual_report":
            output = {
                target: {
                    "headline": "Daily AI" if target == "en" else "每日 AI",
                    "overview": "Metadata overview.",
                    "items": [
                        {
                            "article_id": article["article_id"],
                            "title": article["title"],
                            "summary": "Brief metadata summary.",
                        }
                        for article in input_value["articles"]
                    ],
                    "language": target,
                    "limitations": "Metadata only.",
                }
                for target in ("en", "zh-CN")
            }
        elif request.schema_name == "article_translation":
            output = {
                "title": "文章标题",
                "publisher_summary": "发布方简介",
                "language": language,
                "limitations": "",
            }
        elif request.schema_name == "article_summary_translation":
            output = {
                "summary": {
                    "summary": "文章摘要",
                    "key_points": ["要点"],
                    "language": language,
                    "basis": "metadata",
                    "limitations": "仅依据元数据",
                },
                "translation": {
                    "title": "文章标题",
                    "publisher_summary": "发布方简介",
                    "language": language,
                    "limitations": "",
                },
            }
        else:
            output = {
                "headline": "Daily AI" if language == "en" else "每日 AI",
                "overview": "Metadata overview.",
                "items": [
                    {
                        "article_id": article["article_id"],
                        "title": article["title"],
                        "summary": "Brief metadata summary.",
                    }
                    for article in input_value["articles"]
                ],
                "language": language,
                "limitations": "Metadata only.",
            }
        return ProviderResponse(
            output_text=json.dumps(output, ensure_ascii=False),
            usage=ProviderUsage(
                input_tokens=50,
                cached_input_tokens=10,
                output_tokens=20,
                total_tokens=70,
            ),
            model=(
                "test-provider/routed-free-model:free"
                if request.model == OPENROUTER_MODEL
                else DEEPSEEK_MODEL
            ),
            request_id="safe-request-id",
        )

    def base_args(self):
        return ["--config", str(self.config_path)]

    def test_default_cloud_run_uses_openrouter_and_is_cache_aware(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        outputs = []
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("DeepSeek must not be used"),
        ) as deepseek_generate:
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(
                        self.base_args()
                        + ["ai", "cloud-run", "--yes", "--json"]
                    )
                self.assertEqual(0, status)
                outputs.append(json.loads(output.getvalue()))

        first, second = outputs
        self.assertEqual(2, generate.call_count)
        self.assertEqual(0, deepseek_generate.call_count)
        self.assertEqual(2, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        self.assertEqual(2, len(first["reports"]))
        self.assertFalse(first["weekly_due"])
        self.assertEqual(1, len(first["article_results"]))
        self.assertEqual(1, first["article_results"][0]["provider_api_calls"])
        self.assertIn(
            "translation_artifact_id",
            first["article_results"][0],
        )
        self.assertNotIn(
            "summary_artifact_id",
            first["article_results"][0],
        )
        self.assertEqual(1, second["coverage_cache_hits"])
        stored = Database(self.database_path).latest_ai_artifacts([self.article_id])
        self.assertEqual(
            ["translation"],
            [artifact["task_type"] for artifact in stored[self.article_id]],
        )
        self.assertEqual(
            {
                "requests": 2,
                "confirmed_requests": 2,
                "unconfirmed_requests": 0,
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "cache_miss_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 40,
                "reasoning_tokens": 0,
                "total_tokens": 140,
                "reserved_total_tokens_for_unconfirmed": 0,
            },
            first["usage"],
        )
        self.assertEqual(0, second["usage"]["requests"])
        requests = [call.args[0] for call in generate.call_args_list]
        self.assertTrue(all(request.model == OPENROUTER_MODEL for request in requests))
        self.assertTrue(all(request.reasoning_effort == "none" for request in requests))
        self.assertEqual(
            1,
            sum(request.schema_name == "article_translation" for request in requests),
        )
        self.assertNotIn(
            "article_summary_translation",
            [request.schema_name for request in requests],
        )
        self.assertNotIn(
            "article_summary",
            [request.schema_name for request in requests],
        )
        self.assertEqual(
            1,
            sum(request.schema_name == "bilingual_report" for request in requests),
        )
        self.assertEqual(
            {"extra_network_call": False, "validation": "fixed chat-completions request"},
            first["model_preflight"],
        )
        self.assertEqual("openrouter", first["primary_provider"])
        self.assertEqual("deepseek", first["fallback_provider"])
        self.assertFalse(first["fallback_activated"])
        self.assertFalse(first["degraded"])
        self.assertEqual({"openrouter": 2}, first["provider_api_calls_by_provider"])

    def test_cloud_run_openrouter_uses_fixed_profile_and_reports_selection(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "openrouter-test"}, clear=True
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("DeepSeek must not be used"),
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args()
                + [
                    "ai",
                    "cloud-run",
                    "--provider",
                    "openrouter",
                    "--yes",
                    "--json",
                ]
            )

        self.assertEqual(0, status)
        self.assertEqual(2, openrouter_generate.call_count)
        self.assertEqual(0, deepseek_generate.call_count)
        requests = [call.args[0] for call in openrouter_generate.call_args_list]
        self.assertTrue(all(request.model == OPENROUTER_MODEL for request in requests))
        result = json.loads(output.getvalue())
        self.assertEqual("openrouter", result["provider"])
        self.assertEqual(OPENROUTER_MODEL, result["model"])
        self.assertEqual("OPENROUTER_API_KEY", result["api_key_environment"])
        self.assertEqual(2, result["provider_api_calls"])
        self.assertTrue(result["completed"])

    def test_openrouter_429_trips_one_run_circuit_and_deepseek_completes(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "OpenRouter returned HTTP 429",
                status=429,
                retryable=True,
            ),
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertTrue(result["completed"])
        self.assertTrue(result["fallback_activated"])
        self.assertTrue(result["degraded"])
        self.assertEqual("deepseek", result["active_provider"])
        self.assertEqual(1, openrouter_generate.call_count)
        self.assertEqual(2, deepseek_generate.call_count)
        self.assertEqual(3, result["provider_api_calls"])
        self.assertEqual(
            {"openrouter": 1, "deepseek": 2},
            result["provider_api_calls_by_provider"],
        )
        self.assertEqual("http_429", result["fallback_events"][0]["reason"])
        self.assertTrue(result["fallback_events"][0]["primary_call_made"])
        self.assertEqual("report", result["fallback_events"][0]["kind"])
        self.assertEqual(
            {"deepseek"},
            {str(report["provider"]) for report in result["reports"]},
        )
        self.assertEqual(
            {DEEPSEEK_MODEL},
            {str(report["resolved_model"]) for report in result["reports"]},
        )
        self.assertEqual(
            [DEEPSEEK_MODEL, DEEPSEEK_MODEL],
            [call.args[0].model for call in deepseek_generate.call_args_list],
        )
        attempts = Database(self.database_path).list_ai_attempts()
        self.assertEqual(
            {"openrouter", "deepseek"},
            {str(attempt["requested_provider"]) for attempt in attempts},
        )
        self.assertEqual([], Database(self.database_path).list_ai_generation_holds())
        stored = Database(self.database_path).latest_ai_artifacts([self.article_id])
        self.assertEqual("deepseek", stored[self.article_id][0]["provider"])

        second_output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=AssertionError("fallback artifacts must be reusable"),
        ) as second_openrouter, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("fallback artifacts must be reusable"),
        ) as second_deepseek, redirect_stdout(second_output):
            second_status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        second = json.loads(second_output.getvalue())
        self.assertEqual(0, second_status)
        self.assertEqual(0, second_openrouter.call_count)
        self.assertEqual(0, second_deepseek.call_count)
        self.assertEqual(0, second["provider_api_calls"])
        self.assertEqual({}, second["provider_api_calls_by_provider"])
        self.assertTrue(all(report["cache_hit"] for report in second["reports"]))
        self.assertEqual(
            {"deepseek"},
            {str(report["provider"]) for report in second["reports"]},
        )
        self.assertEqual(
            {DEEPSEEK_MODEL},
            {str(report["resolved_model"]) for report in second["reports"]},
        )
        self.assertEqual(1, second["coverage_cache_hits"])

    def test_missing_openrouter_key_falls_back_before_primary_attempt(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=AssertionError("OpenRouter transport must not be called"),
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertEqual(0, openrouter_generate.call_count)
        self.assertEqual(2, deepseek_generate.call_count)
        self.assertEqual({"deepseek": 2}, result["provider_api_calls_by_provider"])
        self.assertEqual("missing_api_key", result["fallback_events"][0]["reason"])
        self.assertFalse(result["fallback_events"][0]["primary_call_made"])
        attempts = Database(self.database_path).list_ai_attempts()
        self.assertTrue(attempts)
        self.assertEqual(
            {"deepseek"},
            {str(attempt["requested_provider"]) for attempt in attempts},
        )

    def test_deepseek_fallback_failure_never_loops_or_makes_a_third_call(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "OpenRouter returned HTTP 429",
                status=429,
                retryable=True,
            ),
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "DeepSeek result is unknown",
                status=503,
                retryable=True,
            ),
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(1, status)
        self.assertEqual(1, openrouter_generate.call_count)
        self.assertEqual(1, deepseek_generate.call_count)
        self.assertEqual(2, result["provider_api_calls"])
        self.assertEqual(
            {"openrouter": 1, "deepseek": 1},
            result["provider_api_calls_by_provider"],
        )
        self.assertTrue(result["fallback_activated"])
        holds = Database(self.database_path).list_ai_generation_holds()
        self.assertEqual(
            {"fallback_pending", "ambiguous"},
            {str(hold["hold_class"]) for hold in holds},
        )

    def test_ambiguous_openrouter_failure_never_calls_deepseek(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "OpenRouter result is unknown",
                status=503,
                retryable=True,
            ),
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("ambiguous failure must not fall back"),
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(1, status)
        self.assertEqual(1, openrouter_generate.call_count)
        self.assertEqual(0, deepseek_generate.call_count)
        self.assertFalse(result["fallback_activated"])
        self.assertFalse(result["degraded"])
        self.assertEqual({"openrouter": 1}, result["provider_api_calls_by_provider"])
        self.assertEqual(1, result["usage"]["unconfirmed_requests"])
        holds = Database(self.database_path).list_ai_generation_holds()
        self.assertEqual(1, len(holds))
        self.assertEqual("ambiguous", holds[0]["hold_class"])

    def test_openrouter_refusal_never_uses_fallback(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        refusal = ProviderKnownError(
            "provider refused the completion",
            code="content_filter",
            usage=ProviderUsage(input_tokens=12, output_tokens=1, total_tokens=13),
            model="routed-model",
            request_id="request-refusal",
            response_id="response-refusal",
        )
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=refusal,
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("safety refusal must not fall back"),
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(1, status)
        self.assertEqual(1, openrouter_generate.call_count)
        self.assertEqual(0, deepseek_generate.call_count)
        self.assertFalse(result["fallback_activated"])
        self.assertEqual(1, result["usage"]["confirmed_requests"])
        holds = Database(self.database_path).list_ai_generation_holds()
        self.assertEqual(1, len(holds))
        self.assertEqual("paid_failure", holds[0]["hold_class"])

    def test_known_invalid_openrouter_output_falls_back_once(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        invalid_response = ProviderResponse(
            output_text=json.dumps({"not": "the bilingual schema"}),
            usage=ProviderUsage(input_tokens=15, output_tokens=5, total_tokens=20),
            model="routed-model",
            request_id="request-invalid",
        )
        output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            return_value=invalid_response,
        ) as openrouter_generate, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as deepseek_generate, redirect_stdout(output):
            status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertEqual(1, openrouter_generate.call_count)
        self.assertEqual(2, deepseek_generate.call_count)
        self.assertEqual(
            "invalid_bilingual_structured_output",
            result["fallback_events"][0]["reason"],
        )
        self.assertEqual(3, result["usage"]["confirmed_requests"])
        self.assertEqual([], Database(self.database_path).list_ai_generation_holds())

    def test_budget_blocked_fallback_is_resumed_without_replaying_openrouter(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        configuration = json.loads(self.config_path.read_text(encoding="utf-8"))
        configuration["ai"]["budget"]["daily_max_requests"] = 1
        configuration["ai"]["budget"]["monthly_max_requests"] = 1
        self.config_path.write_text(json.dumps(configuration), encoding="utf-8")

        first_output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "OpenRouter returned HTTP 429",
                status=429,
                retryable=True,
            ),
        ) as first_openrouter, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("budget must stop DeepSeek before transport"),
        ) as first_deepseek, redirect_stdout(first_output):
            first_status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        first = json.loads(first_output.getvalue())
        self.assertEqual(1, first_status)
        self.assertEqual(1, first_openrouter.call_count)
        self.assertEqual(0, first_deepseek.call_count)
        self.assertTrue(first["fallback_activated"])
        self.assertTrue(first["budget_exhausted"])
        self.assertEqual({"openrouter": 1}, first["provider_api_calls_by_provider"])
        holds = Database(self.database_path).list_ai_generation_holds()
        self.assertEqual(1, len(holds))
        self.assertEqual("fallback_pending", holds[0]["hold_class"])

        configuration["ai"]["budget"]["daily_max_requests"] = 3
        configuration["ai"]["budget"]["monthly_max_requests"] = 3
        self.config_path.write_text(json.dumps(configuration), encoding="utf-8")

        second_output = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
            },
            clear=True,
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.OpenRouterChatCompletionsProvider.generate",
            side_effect=AssertionError("pending fallback must not replay OpenRouter"),
        ) as second_openrouter, mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as second_deepseek, redirect_stdout(second_output):
            second_status = main(
                self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
            )

        second = json.loads(second_output.getvalue())
        self.assertEqual(0, second_status)
        self.assertEqual(0, second_openrouter.call_count)
        self.assertEqual(2, second_deepseek.call_count)
        self.assertEqual("fallback_pending", second["fallback_events"][0]["reason"])
        self.assertFalse(second["fallback_events"][0]["primary_call_made"])
        self.assertEqual({"deepseek": 2}, second["provider_api_calls_by_provider"])
        self.assertEqual([], Database(self.database_path).list_ai_generation_holds())

    def test_production_workflow_never_cross_wires_provider_secrets(self):
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "update.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}",
            workflow,
        )
        self.assertIn(
            "== 'openrouter' && secrets.OPENROUTER_API_KEY || ''",
            workflow,
        )
        self.assertIn("vars.AI_PROVIDER || 'openrouter'", workflow)
        self.assertIn("default: openrouter", workflow)
        self.assertIn('cron: "15 9,21 * * *"', workflow)
        self.assertIn('timezone: "America/Los_Angeles"', workflow)
        self.assertNotIn('cron: "0 10,22 * * *"', workflow)
        self.assertIn('--provider "$AI_PROVIDER"', workflow)
        self.assertIn("arguments+=(--fallback-provider deepseek)", workflow)
        self.assertNotIn("AI_API_KEY", workflow)
        self.assertNotIn(
            "secrets.OPENROUTER_API_KEY || secrets.DEEPSEEK_API_KEY",
            workflow,
        )

    def test_production_workflow_preserves_ai_failure_through_tee(self):
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "update.yml"
        ).read_text(encoding="utf-8")

        def step(name):
            marker = "      - name: %s\n" % name
            start = workflow.index(marker)
            end = workflow.find("\n      - name: ", start + len(marker))
            return workflow[start:] if end < 0 else workflow[start:end]

        ai_name = "Generate only missing or changed language artifacts"
        ai_step = step(ai_name)
        self.assertIn("        id: ai\n", ai_step)
        self.assertIn("        continue-on-error: true\n", ai_step)
        self.assertIn("        shell: bash\n", ai_step)
        self.assertIn("          set -o pipefail\n", ai_step)
        self.assertIn(
            '          ./aaron-reader "${arguments[@]}" | tee '
            "data/cloud-ai-run.json\n",
            ai_step,
        )

        release_step = step("Build and commit the exact public state")
        self.assertIn("        shell: bash\n", release_step)
        self.assertIn("          set -o pipefail\n", release_step)
        self.assertIn(
            "            | tee data/cloud-release.json\n",
            release_step,
        )

        sentinel_name = "Mark an incomplete AI cycle after preserving valid progress"
        sentinel_step = step(sentinel_name)
        self.assertIn("        if: steps.ai.outcome == 'failure'\n", sentinel_step)
        self.assertIn("          exit 1\n", sentinel_step)
        ordered_steps = [
            ai_name,
            "Export and independently validate both public handoffs",
            "Build and commit the exact public state",
            "Push only the verified commit",
            "Verify Cloudflare published the exact snapshot",
            "Write a redacted run summary",
            sentinel_name,
        ]
        self.assertEqual(
            sorted(workflow.index("      - name: %s" % name) for name in ordered_steps),
            [workflow.index("      - name: %s" % name) for name in ordered_steps],
        )

        run_marker = "        run: |\n"
        run_script = textwrap.dedent(
            "\n".join(
                line[10:] if line.startswith("          ") else line
                for line in ai_step.split(run_marker, 1)[1].splitlines()
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            executable = root / "aaron-reader"
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' '{\"completed\":false}'\n"
                "exit 1\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            environment = {
                **os.environ,
                "AI_PROVIDER": "deepseek",
                "FORCE_WEEKLY": "false",
                "FORCE_HELD": "false",
            }
            completed = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-c", run_script],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(1, completed.returncode)
            self.assertEqual(
                {"completed": False},
                json.loads(
                    (root / "data" / "cloud-ai-run.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )

    def test_cloud_run_requires_confirmation_and_fails_fast_after_one_error(self):
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=AssertionError("must not call without --yes"),
        ) as generate, redirect_stderr(io.StringIO()):
            rejected = main(self.base_args() + ["ai", "cloud-run", "--json"])
        self.assertEqual(2, rejected)
        self.assertEqual(0, generate.call_count)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=ProviderHTTPError(
                "provider returned HTTP 429",
                status=429,
                retryable=True,
            ),
        ) as generate, redirect_stdout(output):
            failed = main(
                self.base_args()
                + [
                    "ai",
                    "cloud-run",
                    "--provider",
                    "deepseek",
                    "--fallback-provider",
                    "none",
                    "--yes",
                    "--json",
                ]
            )
        self.assertEqual(1, failed)
        self.assertEqual(1, generate.call_count)
        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual([], result["article_results"])
        self.assertEqual(1, result["usage"]["requests"])
        self.assertEqual(1, result["usage"]["unconfirmed_requests"])

    def test_cloud_run_preserves_hold_without_rebilling_and_stays_failed(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        arguments = self.base_args() + [
            "ai",
            "cloud-run",
            "--provider",
            "deepseek",
            "--fallback-provider",
            "none",
            "--yes",
            "--json",
        ]
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=ProviderUnknownError("result unknown"),
        ) as first_generate, redirect_stdout(io.StringIO()):
            self.assertEqual(1, main(arguments))
        self.assertEqual(1, first_generate.call_count)

        output = io.StringIO()
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as second_generate, redirect_stdout(output):
            status = main(arguments)

        result = json.loads(output.getvalue())
        self.assertEqual(1, status)
        self.assertFalse(result["completed"])
        self.assertEqual(1, result["generation_holds_skipped"])
        self.assertEqual([], result["failures"])
        self.assertEqual("generation_hold", result["reports"][0]["skipped"])
        self.assertEqual(1, second_generate.call_count)

    def test_weekly_report_is_only_due_sunday_night_or_when_forced(self):
        self.assertFalse(
            _weekly_report_due(
                datetime(2026, 8, 3, 2, 59, tzinfo=timezone.utc)
            )
        )
        self.assertTrue(
            _weekly_report_due(
                datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
            )
        )
        self.assertTrue(
            _weekly_report_due(
                datetime(2026, 8, 1, 5, 0, tzinfo=timezone.utc),
                force=True,
            )
        )

    def test_normal_status_never_constructs_ai_provider(self):
        with mock.patch(
            "aaron_reader.ai_service.DeepSeekChatCompletionsProvider",
            side_effect=AssertionError("normal status must stay zero-token"),
        ), redirect_stdout(io.StringIO()):
            result = main(self.base_args() + ["status"])
        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
