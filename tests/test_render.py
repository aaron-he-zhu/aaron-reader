import json
import re
import sys
import tempfile
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.database import Database, utc_now  # noqa: E402
from aaron_reader.models import ArticleCandidate, SourceConfig  # noqa: E402
from aaron_reader.normalize import clean_text, stable_hash  # noqa: E402
from aaron_reader.render import (  # noqa: E402
    build_llm_packet,
    render_digest,
    render_feed,
    render_html,
    render_json,
    render_outputs,
)


class RenderTests(unittest.TestCase):
    def test_all_outputs_and_packet_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "reader.sqlite3")
            database.initialize()
            source = SourceConfig(
                slug="example",
                name="Example & Co",
                home_url="https://example.com/blog",
                fetch_url="https://example.com/feed.xml",
                adapter="rss",
            )
            database.sync_source_configs([source])
            candidate = ArticleCandidate(
                source_slug="example",
                external_id="one",
                url="https://example.com/blog/one?x=1&y=2",
                title="A <safe> title",
                summary="Official & deterministic description",
                published_at="2026-07-31T00:00:00Z",
                content_hash=stable_hash("one"),
            )
            database.commit_candidates(
                source,
                [candidate],
                started_at=utc_now(),
                http_status=200,
                etag="",
                last_modified="",
                body_hash="one",
                force_history_unread=True,
                listing_item_count=1,
            )
            output = root / "public"
            render_outputs(database, output)

            self.assertEqual(
                {"index.html", "latest.json", "feed.xml", "digest.md"},
                {path.name for path in output.iterdir()},
            )
            page = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("A &lt;safe&gt; title", page)
            self.assertNotIn("A <safe> title", page)
            self.assertIn('<html lang="en">', page)
            self.assertIn(
                "Articles: 1 · Unread: 1 · Showing latest: 1 (limit: 500)",
                page,
            )
            payload = json.loads((output / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual("en", payload["language"])
            self.assertEqual(["en", "zh-CN"], payload["supported_languages"])
            self.assertEqual(0, payload["llm_tokens_used"])
            self.assertEqual({"total": 1, "unread": 1, "starred": 0}, payload["counts"])
            self.assertEqual(
                {
                    "order": "newest_first",
                    "returned_count": 1,
                    "limit": 500,
                    "omitted_count": 0,
                    "truncated": False,
                },
                payload["articles_page"],
            )
            self.assertEqual(1, len(payload["articles"]))

            digest = (output / "digest.md").read_text(encoding="utf-8")
            self.assertIn("# Aaron Reader Unread Digest", digest)
            self.assertIn(
                "Showing the latest 1 of 1 unread articles (limit: 100).",
                digest,
            )
            self.assertIn(
                r"[A \<safe\> title](<https://example.com/blog/one?x=1&y=2>)",
                digest,
            )

            feed_root = ET.fromstring((output / "feed.xml").read_bytes())
            self.assertEqual("en", feed_root.findtext("./channel/language"))
            self.assertEqual(
                "Deterministic AI lab subscriptions",
                feed_root.findtext("./channel/description"),
            )

            packet = build_llm_packet(database.list_articles(unread_only=True), max_chars=800)
            self.assertEqual("en", packet["language"])
            self.assertEqual(["en", "zh-CN"], packet["supported_languages"])
            self.assertIn("Send this object to an LLM only when necessary", packet["instruction_hint"])
            self.assertEqual(1, packet["article_count"])
            self.assertEqual(1, packet["input_article_count"])
            self.assertEqual(0, packet["omitted_article_count"])
            self.assertEqual(0, packet["truncated_summary_count"])
            self.assertFalse(packet["truncated"])
            serialized = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
            self.assertLessEqual(packet["character_count"], 800)
            self.assertEqual(len(serialized), packet["character_count"])
            self.assertEqual(len(serialized.encode("utf-8")), packet["utf8_bytes"])

            chinese_output = root / "public-zh"
            render_outputs(database, chinese_output, language="zh-CN")
            chinese_page = (chinese_output / "index.html").read_text(encoding="utf-8")
            self.assertIn('<html lang="zh-CN">', chinese_page)
            self.assertIn("总计 1 篇 · 1 未读 · 页面显示最近 1 篇（上限 500）", chinese_page)
            chinese_payload = json.loads(
                (chinese_output / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("zh-CN", chinese_payload["language"])
            self.assertIn(
                "列出最近 1 / 1 篇未读文章（上限 100）。",
                (chinese_output / "digest.md").read_text(encoding="utf-8"),
            )
            chinese_feed_root = ET.fromstring((chinese_output / "feed.xml").read_bytes())
            self.assertEqual("zh-CN", chinese_feed_root.findtext("./channel/language"))
            self.assertEqual(
                "确定性的 AI 实验室订阅",
                chinese_feed_root.findtext("./channel/description"),
            )

    def test_control_characters_and_active_markdown_are_neutralized(self) -> None:
        controls = "\x00\x01\x1b\x7f\x85\x9f"
        self.assertEqual("alpha beta gamma", clean_text("alpha\x00beta\tgamma"))
        self.assertFalse(any(character in clean_text("safe" + controls) for character in controls))

        article = {
            "id": 7,
            "source_slug": "example\x00source",
            "source_name": "Example <script>alert(1)</script>\x85",
            "canonical_url": "https://example.com/post?q=<tag>&x=1",
            "title": "![pixel](https://tracker.example/p.gif) <img src='https://tracker.example/x'>\x1b",
            "summary": "<script>fetch('https://tracker.example')</script> ![remote](https://tracker.example/r.png)\x00",
            "category": "Security\x7f",
            "published_at": "2026-08-01T00:00:00Z",
            "discovered_at": "2026-08-01T00:00:00Z",
            "read_at": None,
            "starred_at": None,
        }

        digest = render_digest([article], total_count=1, article_limit=100)
        self.assertNotIn("![pixel]", digest)
        self.assertNotIn("![remote]", digest)
        self.assertNotIn("<img src=", digest)
        self.assertNotIn("<script>", digest)
        self.assertIn(r"\!\[pixel\]\(https\:\/\/tracker\.example\/p\.gif\)", digest)
        self.assertIn("%3Ctag%3E", digest)

        page = render_html(
            [article],
            [
                {
                    "slug": "example",
                    "name": "Example\x00",
                    "health": "degraded",
                    "pending_count": 2,
                    "article_count": 1,
                    "unread_count": 1,
                    "last_success_at": "2026-08-01T00:00:00Z",
                    "last_error": "",
                }
            ],
            counts={"total": 1, "unread": 1, "starred": 0},
            article_limit=500,
            language="zh-CN",
        )
        self.assertNotIn("\x00", page)
        self.assertNotIn("\x85", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src='https://tracker.example/x'>", page)
        self.assertIn("&lt;img src=&#x27;https://tracker.example/x&#x27;&gt;", page)
        self.assertIn(
            'class="health degraded"><span data-i18n="health_degraded">降级</span>',
            page,
        )
        self.assertIn(
            'data-i18n-template="source_counts" data-articles="1" '
            'data-unread="1">1 篇 / 1 未读</span>',
            page,
        )
        self.assertIn(
            'data-i18n-template="pending" data-pending="2"> · 2 待补全</span>',
            page,
        )
        self.assertIn("%3Ctag%3E", page)

        latest = render_json(
            [article],
            [],
            counts={"total": 1, "unread": 1, "starred": 0},
            article_limit=500,
        )
        self.assertFalse(any(character in latest for character in controls))
        self.assertEqual(
            "https://example.com/post?q=%3Ctag%3E&x=1",
            json.loads(latest)["articles"][0]["url"],
        )

        feed = render_feed([article])
        ET.fromstring(feed.encode("utf-8"))
        self.assertFalse(any(character in feed for character in controls))
        self.assertNotIn("<img src='https://tracker.example/x'>", feed)
        self.assertIn("&lt;img src='https://tracker.example/x'&gt;", feed)

    def test_outputs_use_full_database_counts_and_digest_queries_unread_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "reader.sqlite3")
            database.initialize()
            source = SourceConfig(
                slug="example",
                name="Example",
                home_url="https://example.com/blog",
                fetch_url="https://example.com/feed.xml",
                adapter="rss",
            )
            database.sync_source_configs([source])
            candidates = [
                ArticleCandidate(
                    source_slug="example",
                    external_id="story-%d" % index,
                    url="https://example.com/blog/story-%d" % index,
                    title="Story %d" % index,
                    content_hash=stable_hash("story-%d" % index),
                )
                for index in range(501)
            ]
            database.commit_candidates(
                source,
                candidates,
                started_at=utc_now(),
                http_status=200,
                etag="",
                last_modified="",
                body_hash="all-stories",
                listing_item_count=501,
            )
            oldest = next(
                article
                for article in database.list_articles(limit=1000)
                if article["title"] == "Story 0"
            )
            database.set_read([int(oldest["id"])], read=False)

            output = root / "public"
            render_outputs(database, output)

            page = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn(
                "Articles: 501 · Unread: 1 · Showing latest: 500 (limit: 500)",
                page,
            )
            self.assertNotIn(">Story 0<", page)

            payload = json.loads((output / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual({"total": 501, "unread": 1, "starred": 0}, payload["counts"])
            self.assertEqual(500, payload["articles_page"]["returned_count"])
            self.assertEqual(1, payload["articles_page"]["omitted_count"])
            self.assertTrue(payload["articles_page"]["truncated"])
            self.assertNotIn("Story 0", {article["title"] for article in payload["articles"]})

            digest = (output / "digest.md").read_text(encoding="utf-8")
            self.assertIn(
                "Showing the latest 1 of 1 unread articles (limit: 100).",
                digest,
            )
            self.assertIn("Story 0", digest)

    def test_html_embeds_complete_runtime_translations_and_language_selector(self) -> None:
        article = {
            "id": 1,
            "source_slug": "healthy",
            "source_name": "Healthy Source",
            "canonical_url": "https://example.com/one",
            "title": "One article",
            "summary": "A summary",
            "category": "Updates",
            "published_at": "2026-08-01T00:00:00Z",
            "discovered_at": "2026-08-01T00:00:00Z",
            "read_at": None,
            "starred_at": None,
        }
        statuses = [
            {
                "slug": health,
                "name": health,
                "health": health,
                "article_count": 1,
                "unread_count": 1,
                "pending_count": 1 if health == "degraded" else 0,
            }
            for health in ("healthy", "never_synced", "stale", "error", "degraded")
        ]
        english_page = render_html(
            [article],
            statuses,
            counts={"total": 1, "unread": 1, "starred": 0},
            article_limit=500,
        )

        self.assertIn('<html lang="en">', english_page)
        self.assertIn('<select id="language">', english_page)
        self.assertIn('<option value="en" selected="selected">English</option>', english_page)
        self.assertIn('data-i18n-placeholder="search_placeholder"', english_page)
        self.assertIn("document.documentElement.lang=nextLanguage", english_page)
        self.assertIn("localStorage.getItem('aaron-reader-language')", english_page)
        self.assertIn("localStorage.setItem('aaron-reader-language',nextLanguage)", english_page)
        self.assertNotIn("fetch(", english_page)
        self.assertNotIn("<script src=", english_page)
        self.assertNotIn("<link rel=", english_page)

        translation_match = re.search(
            r'<script id="aaron-reader-translations" type="application/json">(.*?)</script>',
            english_page,
            re.DOTALL,
        )
        self.assertIsNotNone(translation_match)
        translations = json.loads(translation_match.group(1))
        self.assertEqual({"en", "zh-CN"}, set(translations))
        required_ui_keys = {
            "eyebrow",
            "subtitle",
            "counter",
            "health_healthy",
            "health_never_synced",
            "health_stale",
            "health_error",
            "health_degraded",
            "source_counts",
            "pending",
            "language_label",
            "search_placeholder",
            "all_sources",
            "all_statuses",
            "unread_only",
            "unread_badge",
            "empty",
            "generated_footer",
        }
        for language in ("en", "zh-CN"):
            self.assertTrue(required_ui_keys.issubset(translations[language]))
        for key in required_ui_keys - {"search_placeholder"}:
            attribute = (
                "data-i18n-template"
                if key in {"counter", "source_counts", "pending", "generated_footer"}
                else "data-i18n"
            )
            self.assertIn('%s="%s"' % (attribute, key), english_page)

        chinese_page = render_html(
            [article],
            statuses,
            counts={"total": 1, "unread": 1, "starred": 0},
            article_limit=500,
            language="zh-CN",
        )
        self.assertIn('<html lang="zh-CN">', chinese_page)
        self.assertIn('<option value="zh-CN" selected="selected">简体中文</option>', chinese_page)
        self.assertIn("搜索标题、简介或类别", chinese_page)
        self.assertIn("没有符合条件的文章。", chinese_page)
        self.assertIn("总计 1 篇 · 1 未读 · 页面显示最近 1 篇（上限 500）", chinese_page)

        self.assertIn('<html lang="zh-CN">', render_html([], [], language="zh"))
        with self.assertRaisesRegex(ValueError, "unsupported language"):
            render_html([], [], language="fr")

    def test_packet_counts_omissions_truncation_chars_and_bytes_exactly(self) -> None:
        articles = [
            {
                "id": index,
                "source_slug": "example",
                "source_name": "示例",
                "canonical_url": "https://example.com/%d" % index,
                "title": "文章 %d\x00" % index,
                "summary": ("中文摘要🙂" * 300) + "\x85",
                "category": "",
                "published_at": "2026-08-01T00:00:00Z",
                "read_at": None,
                "starred_at": None,
            }
            for index in range(3)
        ]
        for language, instruction_fragment in (
            ("en", "Send this object to an LLM only when necessary"),
            ("zh-CN", "仅在确有需要时把本对象交给 LLM"),
        ):
            with self.subTest(language=language):
                packet = build_llm_packet(articles, max_chars=900, language=language)
                serialized = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"

                self.assertEqual(language, packet["language"])
                self.assertEqual(["en", "zh-CN"], packet["supported_languages"])
                self.assertIn(instruction_fragment, packet["instruction_hint"])
                self.assertEqual(3, packet["input_article_count"])
                self.assertEqual(
                    packet["input_article_count"],
                    packet["article_count"] + packet["omitted_article_count"],
                )
                self.assertGreaterEqual(packet["truncated_summary_count"], 1)
                self.assertTrue(packet["truncated"])
                self.assertEqual(900, len(serialized))
                self.assertEqual(len(serialized), packet["character_count"])
                self.assertEqual(
                    len(serialized.encode("utf-8")),
                    packet["utf8_bytes"],
                )
                self.assertNotIn("\x00", serialized)
                self.assertNotIn("\x85", serialized)

        with self.assertRaisesRegex(ValueError, "too small"):
            build_llm_packet(articles, max_chars=100)


if __name__ == "__main__":
    unittest.main()
