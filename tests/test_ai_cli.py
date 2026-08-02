import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_provider import (  # noqa: E402
    ProviderResponse,
    ProviderUsage,
)
from aaron_reader.cli import main  # noqa: E402
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
                    "notification_enabled": False,
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

    def base_args(self):
        return ["--config", str(self.config_path)]

    def test_ai_status_and_preview_do_not_construct_or_call_a_provider(self):
        output = io.StringIO()
        with mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("provider must remain lazy"),
        ), redirect_stdout(output):
            status = main(self.base_args() + ["ai", "status", "--json"])
        self.assertEqual(0, status)
        self.assertEqual(0, json.loads(output.getvalue())["attempts"])

        output = io.StringIO()
        with mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("preview must not construct provider"),
        ), redirect_stdout(output):
            preview = main(
                self.base_args()
                + ["ai", "preview", str(self.article_id), "--task", "summary"]
            )
        self.assertEqual(0, preview)
        self.assertFalse(json.loads(output.getvalue())["provider_will_be_called"])

    def test_non_json_ai_status_renders_api_key_state(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), redirect_stdout(output):
            result = main(self.base_args() + ["ai", "status"])
        self.assertEqual(0, result)
        self.assertIn("API key present: False", output.getvalue())

    def test_subscription_bridge_needs_no_api_key_and_is_idempotent(self):
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["ai"]["enabled"] = False
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        request_path = self.root / "subscription-request.json"
        result_path = self.root / "subscription-request.results.json"

        export_output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("subscription bridge must not construct provider"),
        ), redirect_stdout(export_output):
            exported = main(
                self.base_args()
                + [
                    "ai", "subscription-export", "--all", "--limit", "3",
                    "--to", "zh-CN", "--output", str(request_path),
                ]
            )
        self.assertEqual(0, exported)
        status = json.loads(export_output.getvalue())
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(1, status["pending_count"])
        self.assertEqual(1, request["pending_count"])
        self.assertFalse(request["generation"]["api_key_required"])
        item = request["items"][0]
        self.assertEqual(["summary", "translation"], item["missing_tasks"])

        result_path.write_text(
            json.dumps(
                {
                    "protocol": request["protocol"],
                    "batch_id": request["batch_id"],
                    "target_language": request["target_language"],
                    "items": [
                        {
                            "article_id": item["article_id"],
                            "fingerprint": item["fingerprint"],
                            "summary": {
                                "summary": "文章摘要。",
                                "key_points": ["要点。"],
                                "language": "zh-CN",
                                "basis": "metadata",
                                "limitations": "仅依据发布方元数据。",
                            },
                            "translation": {
                                "title": "文章标题",
                                "publisher_summary": "发布方简介",
                                "language": "zh-CN",
                                "limitations": "",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        invalid_path = self.root / "invalid-subscription-result.json"
        invalid = json.loads(result_path.read_text(encoding="utf-8"))
        invalid["unexpected"] = True
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            rejected = main(
                self.base_args()
                + ["ai", "subscription-import", str(invalid_path), "--json"]
            )
        self.assertEqual(2, rejected)
        self.assertEqual({}, Database(self.database_path).latest_ai_artifacts([self.article_id]))

        imported_output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("subscription bridge must not construct provider"),
        ), redirect_stdout(imported_output):
            first = main(
                self.base_args()
                + ["ai", "subscription-import", str(result_path), "--json"]
            )
            second = main(
                self.base_args()
                + ["ai", "subscription-import", str(result_path), "--json"]
            )
        self.assertEqual((0, 0), (first, second))
        decoder = json.JSONDecoder()
        first_result, offset = decoder.raw_decode(imported_output.getvalue())
        while imported_output.getvalue()[offset].isspace():
            offset += 1
        second_result, _ = decoder.raw_decode(imported_output.getvalue(), offset)
        self.assertEqual(2, first_result["imported_artifacts"])
        self.assertEqual(0, first_result["cache_hits"])
        self.assertEqual(0, second_result["imported_artifacts"])
        self.assertEqual(2, second_result["cache_hits"])
        database = Database(self.database_path)
        self.assertEqual(0, len(database.list_ai_attempts()))
        self.assertEqual(
            2,
            len(database.latest_ai_artifacts([self.article_id])[self.article_id]),
        )

        empty_output = io.StringIO()
        with redirect_stdout(empty_output):
            empty = main(
                self.base_args()
                + ["ai", "subscription-export", "--all", "--limit", "3"]
            )
        self.assertEqual(0, empty)
        self.assertEqual(0, json.loads(empty_output.getvalue())["pending_count"])

    def test_subscription_cli_targets_one_read_article_and_one_task(self):
        database = Database(self.database_path)
        database.set_read([self.article_id], True)
        request_path = self.root / "targeted-request.json"
        result_path = self.root / "targeted-request.results.json"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("targeted subscription export is offline"),
        ), redirect_stdout(io.StringIO()):
            result = main(
                self.base_args()
                + [
                    "ai",
                    "subscription-export",
                    "--article-id",
                    str(self.article_id),
                    "--task",
                    "summary",
                    "--output",
                    str(request_path),
                ]
            )
        self.assertEqual(0, result)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(1, request["pending_count"])
        self.assertEqual({"summary"}, set(request["tasks"]))
        item = request["items"][0]
        self.assertEqual(self.article_id, item["article_id"])
        self.assertEqual(["summary"], item["missing_tasks"])
        result_path.write_text(
            json.dumps(
                {
                    "protocol": request["protocol"],
                    "batch_id": request["batch_id"],
                    "target_language": request["target_language"],
                    "items": [
                        {
                            "article_id": self.article_id,
                            "fingerprint": item["fingerprint"],
                            "summary": {
                                "summary": "目标文章摘要",
                                "key_points": [],
                                "language": "zh-CN",
                                "basis": "metadata",
                                "limitations": "",
                            },
                            "translation": None,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            imported = main(
                self.base_args()
                + ["ai", "subscription-import", str(result_path), "--json"]
            )
        self.assertEqual(0, imported)
        artifacts = Database(self.database_path).latest_ai_artifacts([self.article_id])
        self.assertEqual(["summary"], [item["task_type"] for item in artifacts[self.article_id]])

    def test_subscription_report_cli_imports_and_renders_latest_json(self):
        database = Database(self.database_path)
        with database.connect() as connection:
            connection.execute(
                "UPDATE articles SET published_at=? WHERE id=?",
                (utc_now(), self.article_id),
            )
        request_path = self.root / "daily-report.json"
        result_path = self.root / "daily-report.results.json"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("subscription report export is offline"),
        ), redirect_stdout(io.StringIO()):
            exported = main(
                self.base_args()
                + [
                    "ai",
                    "subscription-report-export",
                    "--period",
                    "daily",
                    "--output",
                    str(request_path),
                ]
            )
        self.assertEqual(0, exported)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(1, request["pending_count"])
        self.assertEqual("America/Los_Angeles", request["timezone"])
        articles = request["input"]["articles"]
        result = {
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
        }
        result["output"] = {
            "headline": "今日博客摘要",
            "overview": "今天共有一篇文章。",
            "items": [
                {
                    "article_id": article["article_id"],
                    "title": "文章",
                    "summary": "文章简介。",
                }
                for article in articles
            ],
            "language": "zh-CN",
            "limitations": "仅使用元数据。",
        }
        result_path.write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("subscription report import is offline"),
        ), redirect_stdout(output):
            imported = main(
                self.base_args()
                + [
                    "ai",
                    "subscription-report-import",
                    str(result_path),
                    "--json",
                ]
            )
        self.assertEqual(0, imported)
        import_status = json.loads(output.getvalue())
        self.assertEqual(1, import_status["imported_artifacts"])
        latest = json.loads(
            (self.output_dir / "latest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, latest["cached_ai_report_count"])
        report = latest["ai_reports"][0]
        self.assertEqual("daily", report["period"])
        self.assertEqual("America/Los_Angeles", report["timezone"])
        self.assertEqual("今日博客摘要", report["output"]["headline"])
        self.assertEqual(0, len(Database(self.database_path).list_ai_attempts()))

    def test_cli_summary_calls_once_then_uses_local_cache(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), mock.patch(
            "aaron_reader.ai_provider.OpenAIResponsesProvider.generate",
            return_value=self.provider_response(),
        ) as generate, redirect_stdout(output):
            first = main(
                self.base_args()
                + ["ai", "summarize", str(self.article_id), "--json"]
            )
            second = main(
                self.base_args()
                + ["ai", "summarize", str(self.article_id), "--json"]
            )
        self.assertEqual((0, 0), (first, second))
        self.assertEqual(1, generate.call_count)
        self.assertIn("CLI summary", output.getvalue())

    def test_batch_and_retry_style_operations_require_yes(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(self.base_args() + ["ai", "batch", "--task", "summary"])
        self.assertEqual(2, result)
        self.assertIn("requires --yes", stderr.getvalue())

    def test_ai_actions_cannot_be_combined_with_network_bind(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--allow-network",
                    "--enable-ai-actions",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("loopback-only", stderr.getvalue())

    def test_normal_status_never_constructs_ai_provider(self):
        with mock.patch(
            "aaron_reader.ai_service.OpenAIResponsesProvider",
            side_effect=AssertionError("normal status must stay zero-token"),
        ), redirect_stdout(io.StringIO()):
            result = main(self.base_args() + ["status"])
        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
