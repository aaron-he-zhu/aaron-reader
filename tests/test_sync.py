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
from aaron_reader.models import AppConfig, FetchResult, SourceConfig  # noqa: E402
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
        self.config = AppConfig(sources=[self.source], notification_enabled=False)

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
        config = AppConfig(sources=[disabled], notification_enabled=False)
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

    def test_notification_outbox_retries_after_delivery_failure(self) -> None:
        config = AppConfig(sources=[self.source], notification_enabled=True)
        first = rss([("one", "First", "Thu, 30 Jul 2026 09:00:00 GMT")])
        second = rss(
            [
                ("two", "Second", "Fri, 31 Jul 2026 09:00:00 GMT"),
                ("one", "First", "Thu, 30 Jul 2026 09:00:00 GMT"),
            ]
        )
        client = FakeClient([first, second, None])
        with mock.patch("aaron_reader.sync.notify_new_articles", return_value=False) as notify:
            sync_all(config, self.database, client=client, notify=True)
            sync_all(
                config,
                self.database,
                client=client,
                notify=True,
                language="zh-CN",
            )
        self.assertEqual(1, notify.call_count)
        notify.assert_called_once_with(1, {"example": 1}, language="zh-CN")
        self.assertEqual(1, self.database.pending_notifications()["total"])
        with self.database.connect() as connection:
            failure = connection.execute(
                "SELECT last_error FROM notification_outbox"
            ).fetchone()[0]
        self.assertIn("通知未送达", failure)

        with mock.patch("aaron_reader.sync.notify_new_articles", return_value=True) as retry:
            result = sync_all(config, self.database, client=client, notify=True)
        self.assertEqual(0, result.unread_new)
        retry.assert_called_once_with(1, {"example": 1}, language="en")
        self.assertEqual(0, self.database.pending_notifications()["total"])

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
        config = AppConfig(sources=[source], notification_enabled=False)
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
        config = AppConfig(sources=[source], notification_enabled=False)
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
        config = AppConfig(sources=[source], notification_enabled=False)
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
        config = AppConfig(sources=[source], notification_enabled=False)
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
        config = AppConfig(sources=[source], notification_enabled=False)
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
        config = AppConfig(sources=[source], notification_enabled=False)
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


if __name__ == "__main__":
    unittest.main()
