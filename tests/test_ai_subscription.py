import json
import sys
import tempfile
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_prompts import stable_hash  # noqa: E402
from aaron_reader.ai_service import AIInputError, AIService  # noqa: E402
from aaron_reader.ai_subscription import (  # noqa: E402
    export_subscription_batch,
    import_subscription_results,
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
        self.candidates = candidates
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

    def test_translation_normalizes_null_for_an_empty_requested_summary(self):
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
        payload["items"][0]["translation"]["publisher_summary"] = None

        result = import_subscription_results(self.service, payload)

        self.assertEqual(1, result["imported_artifacts"])
        artifacts = self.database.latest_ai_artifacts([int(article["id"])])
        output = json.loads(artifacts[int(article["id"])][0]["output_json"])
        self.assertEqual("", output["publisher_summary"])

    def test_translation_rejects_null_for_a_nonempty_requested_summary(self):
        article = self.database.list_articles(limit=1)[0]
        request = export_subscription_batch(
            self.service,
            [article],
            target_language="zh-CN",
            tasks=("translation",),
        )
        self.assertTrue(request["items"][0]["input"]["publisher_summary"])
        payload = self.article_result(
            request,
            include_summary=False,
            include_translation=True,
        )
        payload["items"][0]["translation"]["publisher_summary"] = None

        with self.assertRaisesRegex(
            AIInputError,
            "publisher_summary must be a string",
        ):
            import_subscription_results(self.service, payload)
        self.assertEqual({}, self.database.latest_ai_artifacts([int(article["id"])]))

if __name__ == "__main__":
    unittest.main()
