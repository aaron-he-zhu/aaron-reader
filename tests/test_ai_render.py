import json
import sys
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.render import render_html, render_json  # noqa: E402


def article_with_ai():
    article = {
        "id": 7,
        "source_slug": "example",
        "source_name": "Example",
        "canonical_url": "https://example.com/article",
        "title": "Publisher title",
        "summary": "Publisher summary",
        "category": "News",
        "published_at": "2026-08-01T00:00:00Z",
        "discovered_at": "2026-08-01T00:00:00Z",
        "read_at": None,
        "starred_at": None,
    }
    article["ai_artifacts"] = [
        {
            "id": 11,
            "task_type": "summary",
            "input_scope": "metadata",
            "target_language": "zh-CN",
            "created_at": "2026-08-01T01:00:00Z",
            "input_truncated": 0,
            "provider": "openai",
            "requested_model": "test-model",
            "resolved_model": "test-model-snapshot",
            "output_json": json.dumps(
                {
                    "summary": "<script>alert(1)</script> 中文摘要",
                    "key_points": ["安全重点"],
                    "language": "zh-CN",
                    "basis": "metadata",
                    "limitations": "",
                },
                ensure_ascii=False,
            ),
        },
        {
            "id": 12,
            "task_type": "translation",
            "input_scope": "metadata",
            "target_language": "zh-CN",
            "created_at": "2026-08-01T01:01:00Z",
            "input_truncated": 0,
            "provider": "openai",
            "requested_model": "test-model",
            "resolved_model": "test-model-snapshot",
            "output_json": json.dumps(
                {
                    "title": "发布方标题译文",
                    "publisher_summary": "发布方简介译文",
                    "language": "zh-CN",
                    "limitations": "",
                },
                ensure_ascii=False,
            ),
        },
    ]
    return article


class AIRenderTests(unittest.TestCase):
    def test_cached_ai_is_labeled_escaped_and_never_replaces_publisher_text(self):
        page = render_html([article_with_ai()], [], language="en")
        self.assertIn("Publisher title", page)
        self.assertIn("Publisher summary", page)
        self.assertIn("AI summary", page)
        self.assertIn("Machine translation", page)
        self.assertIn("发布方标题译文", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("fetch(", page)

    def test_latest_json_keeps_ai_in_an_independent_field(self):
        payload = json.loads(render_json([article_with_ai()], []))
        public_article = payload["articles"][0]
        self.assertEqual("Publisher title", public_article["title"])
        self.assertEqual("Publisher summary", public_article["summary"])
        self.assertEqual(2, len(public_article["ai_artifacts"]))
        self.assertNotIn("provider", public_article["ai_artifacts"][0])
        self.assertNotIn("model", public_article["ai_artifacts"][0])
        self.assertEqual(2, payload["cached_ai_artifact_count"])
        self.assertEqual(0, payload["render_llm_tokens_used"])


if __name__ == "__main__":
    unittest.main()
