import hashlib
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.database import Database  # noqa: E402
from aaron_reader.http_client import FetchError  # noqa: E402
from aaron_reader.models import AppConfig, ArticleCandidate, FetchResult, SourceConfig  # noqa: E402
from aaron_reader.sync import SyncAlreadyRunning, sync_all, sync_lock  # noqa: E402


def rss(items) -> bytes:
    parts = ["<?xml version='1.0'?><rss version='2.0'><channel><title>Test</title>"]
    for slug, title, date in items:
        parts.append(
            "<item><guid>%s</guid><link>https://example.com/blog/%s</link>"
            "<title>%s</title><description>Official %s</description><pubDate>%s</pubDate></item>"
            % (slug, slug, title, title, date)
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


class FakeClient:
    def __init__(self, bodies) -> None:
        self.bodies = list(bodies)
        self.calls = []

    def fetch(self, url, etag="", last_modified="", attempts=2, accept=""):
        self.calls.append((url, etag, last_modified))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return FetchResult(url=url, status=304, not_modified=True, etag=etag)
        return FetchResult(
            url=url,
            status=200,
            body=body,
            content_type="application/rss+xml",
            etag='"%s"' % len(body),
            body_hash=hashlib.sha256(body).hexdigest(),
        )


class RoutingClient:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.calls = []

    def fetch(self, url, etag="", last_modified="", attempts=2, accept=""):
        self.calls.append(url)
        body = self.responses[url]
        return FetchResult(
            url=url,
            status=200,
            body=body,
            content_type="text/html",
            body_hash=hashlib.sha256(body).hexdigest(),
        )


class SequencedRoutingClient:
    def __init__(self, responses) -> None:
        self.responses = {url: list(values) for url, values in responses.items()}
        self.calls = []

    def fetch(self, url, etag="", last_modified="", attempts=2, accept=""):
        self.calls.append((url, etag, last_modified))
        value = self.responses[url].pop(0)
        if isinstance(value, Exception):
            raise value
        return FetchResult(
            url=url,
            status=200,
            body=value,
            content_type="text/html",
            etag='"%d"' % len(value),
            body_hash=hashlib.sha256(value).hexdigest(),
        )


def sitemap(entries) -> bytes:
    rows = ["<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"]
    for url, lastmod in entries:
        rows.append("<url><loc>%s</loc><lastmod>%s</lastmod></url>" % (url, lastmod))
    rows.append("</urlset>")
    return "".join(rows).encode("utf-8")


def article_html(url: str, title: str = "New official article") -> bytes:
    return (
        "<!doctype html><html><head>"
        '<link rel="canonical" href="%s">'
        '<meta property="og:title" content="%s">'
        '<meta name="description" content="Official deterministic summary.">'
        '<meta property="article:published_time" content="2026-08-01T10:00:00Z">'
        "</head><body><h1>%s</h1></body></html>" % (url, title, title)
    ).encode("utf-8")


class SitemapClient:
    def __init__(self, source, primary_body, sitemap_bodies, failures=None) -> None:
        self.source = source
        self.primary_body = primary_body
        self.sitemap_bodies = list(sitemap_bodies)
        self.failures = {key: list(value) for key, value in (failures or {}).items()}
        self.calls = []

    def fetch(self, url, etag="", last_modified="", attempts=2, accept=""):
        self.calls.append(url)
        if url == self.source.fetch_url:
            if etag:
                return FetchResult(url=url, status=304, not_modified=True, etag=etag)
            body = self.primary_body
            return FetchResult(
                url=url,
                status=200,
                body=body,
                content_type="text/html",
                etag='"primary"',
                body_hash=hashlib.sha256(body).hexdigest(),
            )
        if url == self.source.sitemap_url:
            if not self.sitemap_bodies:
                return FetchResult(url=url, status=304, not_modified=True, etag=etag)
            body = self.sitemap_bodies.pop(0)
            return FetchResult(
                url=url,
                status=200,
                body=body,
                content_type="application/xml",
                etag='"sitemap-%d"' % len(body),
                body_hash=hashlib.sha256(body).hexdigest(),
            )
        failures = self.failures.get(url, [])
        if failures:
            failure = failures.pop(0)
            if failure is not None:
                raise failure
        body = article_html(url)
        return FetchResult(
            url=url,
            status=200,
            body=body,
            content_type="text/html",
            body_hash=hashlib.sha256(body).hexdigest(),
        )


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "reader.sqlite3")
        self.source = SourceConfig(
            slug="example",
            name="Example",
            home_url="https://example.com/blog",
            fetch_url="https://example.com/feed.xml",
            adapter="rss",
            history_limit=10,
        )
        self.config = AppConfig(sources=[self.source])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_baseline_then_new_item_then_304(self) -> None:
        first = rss([("one", "First", "Thu, 30 Jul 2026 09:00:00 GMT")])
        second = rss(
            [
                ("two", "Second", "Fri, 31 Jul 2026 09:00:00 GMT"),
                ("one", "First", "Thu, 30 Jul 2026 09:00:00 GMT"),
            ]
        )
        client = FakeClient([first, second, None])

        result = sync_all(self.config, self.database, client=client)
        self.assertEqual(1, result.sources[0].seeded)
        self.assertEqual(0, result.unread_new)

        result = sync_all(self.config, self.database, client=client)
        self.assertEqual(1, result.unread_new)
        self.assertEqual(1, self.database.counts()["unread"])

        result = sync_all(self.config, self.database, client=client)
        self.assertEqual("not_modified", result.sources[0].status)
        self.assertEqual(2, self.database.counts()["total"])
        self.assertTrue(client.calls[1][1])

    def test_selection_errors_are_english_by_default_and_chinese_on_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown source"):
            sync_all(
                self.config,
                self.database,
                source_slugs=["missing"],
                client=FakeClient([]),
            )

        disabled = SourceConfig(
            slug="disabled",
            name="Disabled",
            home_url="https://example.com/disabled",
            fetch_url="https://example.com/disabled.xml",
            adapter="rss",
            enabled=False,
        )
        config = AppConfig(sources=[disabled])
        with self.assertRaisesRegex(ValueError, "订阅源已禁用"):
            sync_all(
                config,
                self.database,
                source_slugs=["disabled"],
                client=FakeClient([]),
                language="zh-CN",
            )

    def test_parser_diagnostic_uses_selected_language(self) -> None:
        with mock.patch("aaron_reader.sync.parse_source", return_value=[]):
            result = sync_all(
                self.config,
                self.database,
                client=FakeClient([b"<rss><channel></channel></rss>"]),
                language="zh-CN",
            )
        self.assertEqual("error", result.sources[0].status)
        self.assertIn("解析结果为空", result.sources[0].error)

    def test_sync_lock_uses_selected_language(self) -> None:
        lock_path = Path(self.temporary.name) / "localized.lock"
        with sync_lock(lock_path):
            with self.assertRaisesRegex(SyncAlreadyRunning, "another sync process"):
                with sync_lock(lock_path):
                    pass
            with self.assertRaisesRegex(SyncAlreadyRunning, "另一个同步任务"):
                with sync_lock(lock_path, language="zh-CN"):
                    pass

    def test_same_body_hash_is_noop(self) -> None:
        body = rss([("one", "First", "Thu, 30 Jul 2026 09:00:00 GMT")])
        client = FakeClient([body, body])
        sync_all(self.config, self.database, client=client)
        result = sync_all(self.config, self.database, client=client)
        self.assertEqual("unchanged", result.sources[0].status)
        self.assertEqual(1, self.database.counts()["total"])

    def test_more_than_200_direct_urls_drain_across_runs(self) -> None:
        first = rss([("baseline", "Baseline", "Thu, 30 Jul 2026 09:00:00 GMT")])
        many = [
            (
                "new-%03d" % number,
                "New %03d" % number,
                "Fri, 31 Jul 2026 09:%02d:00 GMT" % (number % 60),
            )
            for number in range(201)
        ]
        second = rss(many + [("baseline", "Baseline", "Thu, 30 Jul 2026 09:00:00 GMT")])
        client = FakeClient([first, second, second])

        sync_all(self.config, self.database, client=client)
        limited = sync_all(self.config, self.database, client=client)
        self.assertEqual(200, limited.unread_new)
        self.assertEqual(1, self.database.pending_url_count(self.source.slug))

        drained = sync_all(self.config, self.database, client=client)
        self.assertEqual(1, drained.unread_new)
        self.assertEqual(0, self.database.pending_url_count(self.source.slug))
        self.assertEqual(202, self.database.counts()["total"])
        self.assertEqual("", client.calls[2][1])

    def test_openai_markdown_index_hydrates_dates_once(self) -> None:
        source = SourceConfig(
            slug="openai-developers",
            name="OpenAI Developer Blog",
            home_url="https://developers.openai.com/blog",
            fetch_url="https://developers.openai.com/blog.md",
            adapter="openai_developers",
            metadata_url="https://developers.openai.com/blog",
        )
        config = AppConfig(sources=[source])
        index = (
            b"# Blog\n## Posts\n"
            b"- [One post](https://developers.openai.com/blog/one.md): Official summary.\n"
        )
        listing = b"""
            <html><body>
              <a class="resource-item" href="/blog/one">
                <img alt="One post">
                <div class="pt-4 text-secondary">Jul 20</div>
                <div class="line-clamp-2">One post</div>
                <p class="line-clamp-3">Official summary.</p>
                <div class="pt-2 text-sm text-secondary">General</div>
              </a>
            </body></html>
        """
        client = RoutingClient(
            {
                source.fetch_url: index,
                source.metadata_url: listing,
            }
        )
        result = sync_all(config, self.database, client=client)
        self.assertFalse(result.failed)
        rows = self.database.list_articles(source_slug=source.slug)
        self.assertEqual("2026-07-20T00:00:00Z", rows[0]["published_at"])
        self.assertEqual(2, len(client.calls))

        result = sync_all(config, self.database, client=client)
        self.assertEqual("unchanged", result.sources[0].status)
        self.assertEqual(3, len(client.calls))

    def test_openai_date_hydration_failure_is_retried(self) -> None:
        source = SourceConfig(
            slug="openai-developers",
            name="OpenAI Developer Blog",
            home_url="https://developers.openai.com/blog",
            fetch_url="https://developers.openai.com/blog.md",
            adapter="openai_developers",
            metadata_url="https://developers.openai.com/blog",
        )
        config = AppConfig(sources=[source])
        index = (
            b"# Blog\n## Posts\n"
            b"- [One post](https://developers.openai.com/blog/one.md): Official summary.\n"
        )
        listing = b"""
            <html><body><a class="resource-item" href="/blog/one">
              <img alt="One post"><div class="pt-4 text-secondary">Jul 20</div>
              <div class="line-clamp-2">One post</div>
              <p class="line-clamp-3">Official summary.</p>
            </a></body></html>
        """
        client = SequencedRoutingClient(
            {
                source.fetch_url: [index, index],
                source.metadata_url: [FetchError("temporary metadata failure", 503), listing],
            }
        )

        first = sync_all(config, self.database, client=client, language="zh-CN")
        self.assertEqual("degraded", first.sources[0].status)
        self.assertIn("日期列表补全失败", first.sources[0].warning)
        self.assertIsNone(self.database.list_articles(source_slug=source.slug)[0]["published_at"])

        second = sync_all(config, self.database, client=client)
        self.assertFalse(second.failed)
        self.assertEqual(
            "2026-07-20T00:00:00Z",
            self.database.list_articles(source_slug=source.slug)[0]["published_at"],
        )
        self.assertEqual("", client.calls[2][1])

    def _anthropic_source(self):
        return SourceConfig(
            slug="anthropic-news",
            name="Anthropic News",
            home_url="https://www.anthropic.com/news",
            fetch_url="https://www.anthropic.com/news",
            adapter="anthropic_news",
            sitemap_url="https://www.anthropic.com/sitemap.xml",
            sitemap_prefix="https://www.anthropic.com/news/",
            sitemap_interval_hours=24,
        )

    def _anthropic_primary(self):
        return (REPOSITORY_ROOT / "tests" / "fixtures" / "anthropic_news.html").read_bytes()

    def test_failed_sitemap_hydration_survives_unchanged_sitemap(self) -> None:
        source = self._anthropic_source()
        config = AppConfig(sources=[source])
        baseline = sitemap(
            [
                ("https://www.anthropic.com/news/claude-opus-5", "2026-07-24"),
                ("https://www.anthropic.com/news/hard-questions", "2026-07-09"),
                ("https://www.anthropic.com/news/investigating-incidents", "2026-07-30"),
                ("https://www.anthropic.com/news", "2026-07-30"),
            ]
        )
        new_url = "https://www.anthropic.com/news/new-story"
        changed = sitemap(
            [
                ("https://www.anthropic.com/news/claude-opus-5", "2026-07-24"),
                ("https://www.anthropic.com/news/hard-questions", "2026-07-09"),
                ("https://www.anthropic.com/news/investigating-incidents", "2026-07-30"),
                (new_url, "2026-08-01"),
            ]
        )
        client = SitemapClient(
            source,
            self._anthropic_primary(),
            [baseline, changed],
            failures={new_url: [FetchError("temporary", 503), None]},
        )

        sync_all(config, self.database, client=client, force=True)
        with self.database.connect() as connection:
            root_seen = connection.execute(
                "SELECT 1 FROM seen_urls WHERE source_slug=? AND url=?",
                (source.slug, "https://www.anthropic.com/news"),
            ).fetchone()
        self.assertIsNone(root_seen)
        failed = sync_all(config, self.database, client=client, force=True)
        self.assertEqual("degraded", failed.sources[0].status)
        self.assertEqual(1, self.database.pending_url_count(source.slug))
        self.assertEqual("degraded", self.database.source_statuses()[0]["health"])

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE pending_urls SET next_attempt_at=NULL WHERE source_slug=?",
                (source.slug,),
            )
        recovered = sync_all(config, self.database, client=client)
        self.assertEqual(1, recovered.unread_new)
        self.assertEqual(0, self.database.pending_url_count(source.slug))
        self.assertEqual("healthy", self.database.source_statuses()[0]["health"])
        self.assertEqual(1, client.calls.count(source.sitemap_url) - 1)

    def test_more_than_25_sitemap_urls_drain_across_runs(self) -> None:
        source = self._anthropic_source()
        config = AppConfig(sources=[source])
        baseline = sitemap(
            [("https://www.anthropic.com/news/investigating-incidents", "2026-07-30")]
        )
        additions = [
            ("https://www.anthropic.com/news/queued-%02d" % number, "2026-08-01")
            for number in range(26)
        ]
        client = SitemapClient(
            source,
            self._anthropic_primary(),
            [baseline, sitemap([("https://www.anthropic.com/news/investigating-incidents", "2026-07-30")] + additions)],
        )

        sync_all(config, self.database, client=client, force=True)
        second = sync_all(config, self.database, client=client, force=True)
        self.assertEqual(25, second.unread_new)
        self.assertEqual(1, self.database.pending_url_count(source.slug))

        third = sync_all(config, self.database, client=client)
        self.assertEqual(1, third.unread_new)
        self.assertEqual(0, self.database.pending_url_count(source.slug))
        self.assertEqual(2, client.calls.count(source.sitemap_url))

    def test_lastmod_refresh_updates_without_new_notification(self) -> None:
        source = self._anthropic_source()
        config = AppConfig(sources=[source])
        url = "https://www.anthropic.com/news/investigating-incidents"
        client = SitemapClient(
            source,
            self._anthropic_primary(),
            [
                sitemap([(url, "2026-07-30")]),
                sitemap([(url, "2026-08-01")]),
            ],
        )
        sync_all(config, self.database, client=client, force=True)
        client.failures[url] = []
        original_fetch = client.fetch

        def revised_fetch(request_url, **kwargs):
            if request_url == url:
                body = article_html(url, "Revised official title")
                return FetchResult(
                    url=url,
                    status=200,
                    body=body,
                    content_type="text/html",
                    body_hash=hashlib.sha256(body).hexdigest(),
                )
            return original_fetch(request_url, **kwargs)

        client.fetch = revised_fetch
        result = sync_all(config, self.database, client=client, force=True)
        self.assertEqual(0, result.unread_new)
        self.assertEqual("Revised official title", self.database.article_by_url(source.slug, url)["title"])
        self.assertEqual(0, self.database.pending_url_count(source.slug))

    def test_429_stops_remaining_article_requests_and_persists_backoff(self) -> None:
        source = self._anthropic_source()
        config = AppConfig(sources=[source])
        baseline_url = "https://www.anthropic.com/news/investigating-incidents"
        first_new = "https://www.anthropic.com/news/a-rate-limited"
        second_new = "https://www.anthropic.com/news/b-waiting"
        client = SitemapClient(
            source,
            self._anthropic_primary(),
            [
                sitemap([(baseline_url, "2026-07-30")]),
                sitemap(
                    [
                        (baseline_url, "2026-07-30"),
                        (first_new, "2026-08-01"),
                        (second_new, "2026-08-01"),
                    ]
                ),
            ],
            failures={
                first_new: [
                    FetchError("rate limited", 429, retry_after_seconds=3600)
                ]
            },
        )

        sync_all(config, self.database, client=client, force=True)
        result = sync_all(config, self.database, client=client, force=True)
        self.assertEqual("degraded", result.sources[0].status)
        self.assertEqual(1, client.calls.count(first_new))
        self.assertEqual(0, client.calls.count(second_new))
        self.assertEqual(2, self.database.pending_url_count(source.slug))
        pending = {row["url"]: row for row in self.database.pending_urls(source.slug, 10)}
        self.assertNotIn(first_new, pending)
        self.assertIn(second_new, pending)

    def test_cursor_zh_locale_stores_publisher_translation_and_skips_ai(self) -> None:
        """Official Chinese locale should skip AI provider for cursor-blog articles."""
        cursor_source = SourceConfig(
            slug="cursor-blog",
            name="Cursor Blog",
            home_url="https://cursor.com/blog",
            fetch_url="https://cursor.com/blog",
            adapter="cursor_blog",
            zh_locale_url="https://cursor.com/cn/blog",
        )
        config = AppConfig(
            sources=[cursor_source],
            database_path=str(self.database.path),
        )
        self.database.initialize()
        self.database.sync_source_configs([cursor_source])

        en_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/blog/test-post">
            <p class="type-md">Test Post Title</p>
            <p class="text-theme-text-sec">English summary of the post.</p>
            <time datetime="2026-08-01">Aug 1, 2026</time>
        </a>
        </body>
        </html>
        """
        zh_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/cn/blog/test-post">
            <p class="type-md">\xe6\xb5\x8b\xe8\xaf\x95\xe6\x96\x87\xe7\xab\xa0\xe6\xa0\x87\xe9\xa2\x98</p>
            <p class="text-theme-text-sec">\xe4\xb8\xad\xe6\x96\x87\xe6\x91\x98\xe8\xa6\x81</p>
        </a>
        </body>
        </html>
        """
        client = RoutingClient({
            "https://cursor.com/blog": en_html,
            "https://cursor.com/cn/blog": zh_html,
        })

        result = sync_all(config, self.database, client=client)
        self.assertEqual("ok", result.sources[0].status)
        self.assertEqual(1, result.sources[0].inserted)

        articles = self.database.list_articles()
        self.assertEqual(1, len(articles))
        article_id = int(articles[0]["id"])
        self.assertEqual("https://cursor.com/blog/test-post", articles[0]["canonical_url"])

        artifacts = self.database.latest_ai_artifacts([article_id])
        self.assertIn(article_id, artifacts)
        artifact_list = artifacts[article_id]
        self.assertEqual(1, len(artifact_list))

        artifact = artifact_list[0]
        self.assertEqual("translation", artifact["task_type"])
        self.assertEqual("zh-CN", artifact["target_language"])
        self.assertEqual("publisher", artifact["provider"])

        import json
        output = json.loads(artifact["output_json"])
        self.assertEqual("测试文章标题", output["title"])
        self.assertEqual("中文摘要", output["publisher_summary"])
        self.assertEqual("zh-CN", output["language"])

    def test_cursor_zh_locale_survives_export_import_roundtrip(self) -> None:
        """Publisher translation must survive export/import and appear in latest_ai_artifacts."""
        from pathlib import Path
        from aaron_reader.ai_cache import export_ai_cache, import_ai_cache
        from aaron_reader.models import AIConfig

        cursor_source = SourceConfig(
            slug="cursor-blog",
            name="Cursor Blog",
            home_url="https://cursor.com/blog",
            fetch_url="https://cursor.com/blog",
            adapter="cursor_blog",
            zh_locale_url="https://cursor.com/cn/blog",
        )
        config = AppConfig(
            sources=[cursor_source],
            database_path=str(self.database.path),
            ai=AIConfig(
                enabled=False,
                provider="deepseek",
                summary_model="deepseek-v4-flash",
                translation_model="deepseek-v4-flash",
                reasoning_effort="none",
                api_key_environment="DEEPSEEK_API_KEY",
            ),
        )
        self.database.initialize()
        self.database.sync_source_configs([cursor_source])

        en_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/blog/roundtrip-test">
            <p class="type-md">Roundtrip Test Article</p>
            <p class="text-theme-text-sec">English summary for roundtrip test.</p>
            <time datetime="2026-08-01">Aug 1, 2026</time>
        </a>
        </body>
        </html>
        """
        zh_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/cn/blog/roundtrip-test">
            <p class="type-md">\xe5\x9b\x9e\xe7\x8e\xaf\xe6\xb5\x8b\xe8\xaf\x95\xe6\x96\x87\xe7\xab\xa0</p>
            <p class="text-theme-text-sec">\xe4\xb8\xad\xe6\x96\x87\xe5\x9b\x9e\xe7\x8e\xaf\xe6\xb5\x8b\xe8\xaf\x95\xe6\x91\x98\xe8\xa6\x81</p>
        </a>
        </body>
        </html>
        """
        client = RoutingClient({
            "https://cursor.com/blog": en_html,
            "https://cursor.com/cn/blog": zh_html,
        })

        result = sync_all(config, self.database, client=client)
        self.assertEqual("ok", result.sources[0].status)

        cache_path = Path(self.temporary.name) / "ai-cache.json"
        export_result = export_ai_cache(self.database, config, cache_path)
        self.assertEqual(1, export_result["article_artifacts"])
        self.assertEqual(0, export_result["skipped_incompatible"])

        import json
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(payload["artifacts"]))
        artifact = payload["artifacts"][0]
        self.assertEqual("publisher", artifact["provider"])
        self.assertEqual("translation", artifact["task_type"])
        self.assertEqual("回环测试文章", artifact["output"]["title"])

        original_article = self.database.list_articles()[0]
        fresh_db = Database(Path(self.temporary.name) / "fresh.sqlite3")
        fresh_db.initialize()
        fresh_db.sync_source_configs([cursor_source])
        fresh_db.commit_candidates(
            cursor_source,
            [
                ArticleCandidate(
                    source_slug="cursor-blog",
                    external_id=str(original_article["external_id"]),
                    url=str(original_article["canonical_url"]),
                    title=str(original_article["title"]),
                    summary=str(original_article["summary"] or ""),
                    published_at=str(original_article["published_at"]),
                    content_hash=str(original_article["content_hash"]),
                )
            ],
            started_at="2026-08-01T17:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="fixture",
        )

        import_result = import_ai_cache(fresh_db, config, cache_path)
        self.assertEqual(1, import_result["inserted_artifacts"])
        self.assertEqual(0, import_result["dropped_untranslated"])

        articles = fresh_db.list_articles()
        fresh_artifacts = fresh_db.latest_ai_artifacts([int(articles[0]["id"])])
        self.assertEqual(1, len(fresh_artifacts.get(int(articles[0]["id"]), [])))
        fresh_artifact = fresh_artifacts[int(articles[0]["id"])][0]
        self.assertEqual("publisher", fresh_artifact["provider"])
        self.assertEqual("回环测试文章", json.loads(fresh_artifact["output_json"])["title"])

    def test_cursor_zh_locale_fetches_per_article_when_listing_missing_slug(self) -> None:
        """Articles not in CN listing but with CN article page should be stored."""
        cursor_source = SourceConfig(
            slug="cursor-blog",
            name="Cursor Blog",
            home_url="https://cursor.com/blog",
            fetch_url="https://cursor.com/blog",
            adapter="cursor_blog",
            zh_locale_url="https://cursor.com/cn/blog",
        )
        config = AppConfig(
            sources=[cursor_source],
            database_path=str(self.database.path),
        )
        self.database.initialize()
        self.database.sync_source_configs([cursor_source])

        en_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/blog/slug-a">
            <p class="type-md">Article A Title</p>
            <p class="text-theme-text-sec">English summary A.</p>
            <time datetime="2026-08-01">Aug 1, 2026</time>
        </a>
        </body>
        </html>
        """
        zh_listing_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <!-- slug-a is NOT in the CN listing -->
        </body>
        </html>
        """
        zh_article_html = b"""
        <!DOCTYPE html>
        <html lang="zh">
        <head>
            <title>\xe6\x96\x87\xe7\xab\xa0 A \xe6\xa0\x87\xe9\xa2\x98 \xc2\xb7 Cursor</title>
            <link rel="canonical" href="https://cursor.com/blog/slug-a"/>
            <meta property="og:title" content="\xe6\x96\x87\xe7\xab\xa0 A \xe6\xa0\x87\xe9\xa2\x98"/>
            <meta property="og:description" content="\xe6\x96\x87\xe7\xab\xa0 A \xe4\xb8\xad\xe6\x96\x87\xe6\x91\x98\xe8\xa6\x81"/>
        </head>
        <body>
            <h1>\xe6\x96\x87\xe7\xab\xa0 A \xe6\xa0\x87\xe9\xa2\x98</h1>
        </body>
        </html>
        """
        client = RoutingClient({
            "https://cursor.com/blog": en_html,
            "https://cursor.com/cn/blog": zh_listing_html,
            "https://cursor.com/cn/blog/slug-a": zh_article_html,
        })

        result = sync_all(config, self.database, client=client)
        self.assertEqual("ok", result.sources[0].status)

        articles = self.database.list_articles()
        self.assertEqual(1, len(articles))
        article_id = int(articles[0]["id"])

        artifacts = self.database.latest_ai_artifacts([article_id])
        self.assertIn(article_id, artifacts)
        artifact_list = artifacts[article_id]
        self.assertEqual(1, len(artifact_list))

        artifact = artifact_list[0]
        self.assertEqual("publisher", artifact["provider"])
        import json
        output = json.loads(artifact["output_json"])
        self.assertEqual("文章 A 标题", output["title"])
        self.assertEqual("文章 A 中文摘要", output["publisher_summary"])

    def test_cursor_zh_locale_skips_english_only_cn_article_page(self) -> None:
        """CN article page with no CJK title should NOT be stored as zh-CN."""
        cursor_source = SourceConfig(
            slug="cursor-blog",
            name="Cursor Blog",
            home_url="https://cursor.com/blog",
            fetch_url="https://cursor.com/blog",
            adapter="cursor_blog",
            zh_locale_url="https://cursor.com/cn/blog",
        )
        config = AppConfig(
            sources=[cursor_source],
            database_path=str(self.database.path),
        )
        self.database.initialize()
        self.database.sync_source_configs([cursor_source])

        en_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <a class="card card--feature" href="/blog/english-only">
            <p class="type-md">English Only Article</p>
            <p class="text-theme-text-sec">English summary.</p>
            <time datetime="2026-08-01">Aug 1, 2026</time>
        </a>
        </body>
        </html>
        """
        zh_listing_html = b"""
        <!DOCTYPE html>
        <html>
        <body>
        <!-- No CN translation in listing -->
        </body>
        </html>
        """
        en_only_cn_page = b"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>English Only Article - Cursor</title>
            <link rel="canonical" href="https://cursor.com/blog/english-only"/>
            <meta property="og:title" content="English Only Article"/>
            <meta property="og:description" content="Still in English on CN page."/>
        </head>
        <body>
            <h1>English Only Article</h1>
        </body>
        </html>
        """
        client = RoutingClient({
            "https://cursor.com/blog": en_html,
            "https://cursor.com/cn/blog": zh_listing_html,
            "https://cursor.com/cn/blog/english-only": en_only_cn_page,
        })

        result = sync_all(config, self.database, client=client)
        self.assertEqual("ok", result.sources[0].status)

        articles = self.database.list_articles()
        self.assertEqual(1, len(articles))
        article_id = int(articles[0]["id"])

        artifacts = self.database.latest_ai_artifacts([article_id])
        self.assertEqual({}, artifacts.get(article_id, {}))


if __name__ == "__main__":
    unittest.main()
