import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_provider import (  # noqa: E402
    DEEPSEEK_MODEL,
    ProviderHTTPError,
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
            model=DEEPSEEK_MODEL,
            request_id="safe-request-id",
        )

    def base_args(self):
        return ["--config", str(self.config_path)]

    def test_cloud_run_is_fixed_combined_cache_aware_and_usage_audited(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 1, 5, 0, tzinfo=timezone.utc)
                return value if tz is None else value.astimezone(tz)

        outputs = []
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "deepseek-test"}, clear=True
        ), mock.patch(
            "aaron_reader.cli.datetime", FixedDateTime
        ), mock.patch(
            "aaron_reader.ai_provider.DeepSeekChatCompletionsProvider.generate",
            side_effect=self.cloud_provider_response,
        ) as generate:
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
        self.assertEqual(2, first["provider_api_calls"])
        self.assertEqual(0, second["provider_api_calls"])
        self.assertEqual(2, len(first["reports"]))
        self.assertFalse(first["weekly_due"])
        self.assertEqual(1, len(first["article_results"]))
        self.assertEqual(1, first["article_results"][0]["provider_api_calls"])
        self.assertEqual(1, second["coverage_cache_hits"])
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
        self.assertTrue(all(request.model == DEEPSEEK_MODEL for request in requests))
        self.assertTrue(all(request.reasoning_effort == "none" for request in requests))
        self.assertEqual(
            1,
            sum(
                request.schema_name == "article_summary_translation"
                for request in requests
            ),
        )
        self.assertEqual(
            1,
            sum(request.schema_name == "bilingual_report" for request in requests),
        )
        self.assertEqual(
            {"extra_network_call": False, "validation": "fixed chat-completions request"},
            first["model_preflight"],
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
                + ["ai", "cloud-run", "--yes", "--json"]
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

        arguments = self.base_args() + ["ai", "cloud-run", "--yes", "--json"]
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
