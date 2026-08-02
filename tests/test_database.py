import sys
import tempfile
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.database import Database, utc_now  # noqa: E402
from aaron_reader.models import ArticleCandidate, SourceConfig  # noqa: E402
from aaron_reader.normalize import stable_hash  # noqa: E402


def source() -> SourceConfig:
    return SourceConfig(
        slug="example",
        name="Example",
        home_url="https://example.com/blog",
        fetch_url="https://example.com/feed.xml",
        adapter="rss",
    )


def article(slug: str, title: str = "Article") -> ArticleCandidate:
    url = "https://example.com/blog/%s" % slug
    return ArticleCandidate(
        source_slug="example",
        external_id=slug,
        url=url,
        title=title,
        summary="Official description",
        published_at="2026-07-30T00:00:00Z",
        content_hash=stable_hash(title, url, "Official description"),
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "reader.sqlite3")
        self.database.initialize()
        self.database.sync_source_configs([source()])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit(self, candidates, **kwargs):
        return self.database.commit_candidates(
            source(),
            candidates,
            started_at=utc_now(),
            http_status=200,
            etag='"feed-1"',
            last_modified="",
            body_hash="body-hash",
            listing_item_count=len(candidates),
            **kwargs,
        )

    def test_first_commit_is_read_backfill_and_second_is_idempotent(self) -> None:
        inserted, updated, seeded, baseline, new_ids = self.commit([article("one"), article("two")])
        self.assertEqual((2, 0, 2, True, []), (inserted, updated, seeded, baseline, new_ids))
        rows = self.database.list_articles()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["read_at"] for row in rows))
        self.assertTrue(all(row["is_backfill"] for row in rows))

        inserted, updated, seeded, baseline, new_ids = self.commit([article("one"), article("two")])
        self.assertEqual((0, 0, 0, False, []), (inserted, updated, seeded, baseline, new_ids))

    def test_new_article_is_unread_and_updates_preserve_user_state(self) -> None:
        self.commit([article("one")])
        inserted, updated, seeded, baseline, new_ids = self.commit(
            [article("one", "Updated title"), article("two", "Brand new")]
        )
        self.assertEqual(1, inserted)
        self.assertEqual(1, updated)
        self.assertFalse(baseline)
        self.assertEqual(1, len(new_ids))
        rows = {row["external_id"]: row for row in self.database.list_articles()}
        self.assertEqual("Updated title", rows["one"]["title"])
        self.assertIsNotNone(rows["one"]["read_at"])
        self.assertIsNone(rows["two"]["read_at"])

        self.assertTrue(self.database.set_starred(int(rows["one"]["id"]), True))
        self.commit([article("one", "Updated again"), article("two", "Brand new")])
        refreshed = self.database.article(int(rows["one"]["id"]))
        self.assertIsNotNone(refreshed["starred_at"])
        self.assertIsNotNone(refreshed["read_at"])

    def test_sitemap_baseline_and_diff(self) -> None:
        baseline, new = self.database.discover_sitemap_urls(
            "example",
            [("https://example.com/blog/one", "2026-07-01")],
        )
        self.assertTrue(baseline)
        self.assertEqual([], new)

        baseline, new = self.database.discover_sitemap_urls(
            "example",
            [
                ("https://example.com/blog/one", "2026-07-02"),
                ("https://example.com/blog/two", "2026-07-03"),
            ],
        )
        self.assertFalse(baseline)
        self.assertEqual(
            [
                ("https://example.com/blog/one", "2026-07-02"),
                ("https://example.com/blog/two", "2026-07-03"),
            ],
            new,
        )
        pending = {
            row["url"]: row for row in self.database.pending_urls("example", 10)
        }
        self.assertEqual("modified", pending["https://example.com/blog/one"]["change_kind"])
        self.assertEqual("new", pending["https://example.com/blog/two"]["change_kind"])
        with self.database.connect() as connection:
            old_lastmod = connection.execute(
                "SELECT remote_modified FROM seen_urls WHERE source_slug=? AND url=?",
                ("example", "https://example.com/blog/one"),
            ).fetchone()[0]
        self.assertEqual("2026-07-01", old_lastmod)
        self.database.mark_seen_urls("example", new)
        self.assertEqual(0, self.database.pending_url_count("example"))
        _, retry = self.database.discover_sitemap_urls(
            "example", [("https://example.com/blog/two", "2026-07-03")]
        )
        self.assertEqual([], retry)

    def test_read_and_star_filters(self) -> None:
        self.commit([article("one")])
        self.commit([article("one"), article("two")])
        rows = self.database.list_articles()
        new_id = int(next(row["id"] for row in rows if row["external_id"] == "two"))
        self.assertEqual(1, len(self.database.list_articles(unread_only=True)))
        self.database.set_starred(new_id, True)
        self.assertEqual(1, len(self.database.list_articles(starred_only=True)))
        self.database.set_read([new_id], True)
        self.assertEqual(0, len(self.database.list_articles(unread_only=True)))

    def test_initialized_slug_cannot_silently_change_source_identity(self) -> None:
        self.commit([article("one")])
        changed = SourceConfig(
            slug="example",
            name="Different site",
            home_url="https://other.example/blog",
            fetch_url="https://other.example/feed.xml",
            adapter="rss",
        )
        with self.assertRaisesRegex(ValueError, "do not reuse its slug"):
            self.database.sync_source_configs([changed])
        with self.assertRaisesRegex(ValueError, "不能复用 slug"):
            self.database.sync_source_configs([changed], language="zh-CN")
        state = self.database.source_state("example")
        self.assertEqual("https://example.com/feed.xml", state["fetch_url"])


if __name__ == "__main__":
    unittest.main()
