import copy
import json
import sys
import tempfile
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.crawler_state import (  # noqa: E402
    CRAWLER_STATE_PROTOCOL,
    _bundle_hash,
    export_crawler_state,
    import_crawler_state,
)
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
        sitemap_url="https://example.com/sitemap.xml",
        sitemap_prefix="https://example.com/blog/",
    )


def article(slug: str, title: str = "Article") -> ArticleCandidate:
    url = "https://example.com/blog/%s" % slug
    summary = "Official description for %s" % slug
    published_at = "2026-07-30T00:00:00Z"
    return ArticleCandidate(
        source_slug="example",
        external_id="external-%s" % slug,
        url=url,
        title=title,
        summary=summary,
        author="Publisher",
        category="Research",
        published_at=published_at,
        content_hash=stable_hash(
            url,
            title,
            summary,
            "Publisher",
            "Research",
            published_at,
            None,
        ),
    )


class CrawlerStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_database = self.database("source.sqlite3")
        self.commit(self.source_database, [article("one")])
        self.source_database.discover_sitemap_urls(
            "example",
            [("https://example.com/blog/one", "2026-07-30")],
        )
        self.source_database.record_http_cache(
            "https://example.com/sitemap.xml",
            200,
            etag='"sitemap-v1"',
            body_hash="a" * 64,
        )
        self.source_database.record_check_success("example", "sitemap")
        self.bundle = self.root / "crawler.json"
        export_crawler_state(self.source_database, [source()], self.bundle)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def database(self, name: str) -> Database:
        database = Database(self.root / name)
        database.initialize()
        database.sync_source_configs([source()])
        return database

    def commit(self, database: Database, articles):
        return database.commit_candidates(
            source(),
            articles,
            started_at=utc_now(),
            http_status=200,
            etag='"feed-v1"',
            last_modified="",
            body_hash="b" * 64,
            listing_item_count=len(articles),
        )

    def payload(self):
        return json.loads(self.bundle.read_text(encoding="utf-8"))

    def write_payload(self, payload, name="modified.json"):
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def test_export_is_versioned_hash_bound_and_public_safe(self) -> None:
        payload = self.payload()
        self.assertEqual(CRAWLER_STATE_PROTOCOL, payload["protocol"])
        self.assertEqual(_bundle_hash(payload), payload["bundle_hash"])
        self.assertEqual(1, len(payload["articles"]))
        self.assertEqual(1, len(payload["seen_urls"]))
        self.assertEqual(1, len(payload["http_cache"]))

        serialized = self.bundle.read_text(encoding="utf-8")
        for forbidden in (
            "read_at",
            "starred_at",
            "last_error",
            "ai_artifacts",
            "ai_reports",
            "ai_jobs",
            "ai_attempts",
            "normalized_text",
            "provider_response_id",
            "api_key",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_seed_requires_empty_database_and_marks_history_read(self) -> None:
        target = self.database("seed.sqlite3")
        result = import_crawler_state(target, [source()], self.bundle, seed=True)
        self.assertEqual("seed", result["mode"])
        self.assertEqual(1, result["inserted"])
        row = target.list_articles()[0]
        self.assertIsNotNone(row["read_at"])
        self.assertEqual(0, len(target.list_articles(unread_only=True)))
        self.assertEqual(0, target.pending_url_count("example"))
        self.assertEqual(
            '"sitemap-v1"',
            target.http_cache("https://example.com/sitemap.xml")["etag"],
        )

        with self.assertRaisesRegex(ValueError, "--seed requires an empty"):
            import_crawler_state(target, [source()], self.bundle, seed=True)

    def test_merge_preserves_read_star_ai_and_report_rows(self) -> None:
        target = self.database("merge.sqlite3")
        self.commit(target, [article("one")])
        row = target.list_articles()[0]
        article_id = int(row["id"])
        target.set_read([article_id], True)
        target.set_starred(article_id, True)
        with target.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_artifacts(
                    article_id, task_type, input_scope, source_language,
                    target_language, artifact_key, input_hash,
                    article_content_hash, prompt_version, prompt_hash,
                    response_schema_version, response_schema_hash, provider,
                    requested_model, resolved_model, generation_params_hash,
                    output_json, output_text, output_hash, status,
                    input_truncated, created_at
                ) VALUES (?, 'summary', 'metadata', 'en', 'zh-CN', ?, ?, ?,
                    'v1', ?, 'v1', ?, 'subscription', 'model', 'model', ?,
                    '{}', 'summary', ?, 'succeeded', 0, ?)
                """,
                (
                    article_id,
                    "c" * 64,
                    "d" * 64,
                    row["content_hash"],
                    "e" * 64,
                    "f" * 64,
                    "1" * 64,
                    "2" * 64,
                    utc_now(),
                ),
            )
            digest_id = connection.execute(
                """
                INSERT INTO ai_artifacts(
                    article_id, task_type, input_scope, source_language,
                    target_language, artifact_key, input_hash,
                    article_content_hash, prompt_version, prompt_hash,
                    response_schema_version, response_schema_hash, provider,
                    requested_model, resolved_model, generation_params_hash,
                    output_json, output_text, output_hash, status,
                    input_truncated, created_at
                ) VALUES (NULL, 'digest', 'digest', 'en', 'en', ?, ?, ?,
                    'v1', ?, 'v1', ?, 'subscription', 'model', 'model', ?,
                    '{}', 'digest', ?, 'succeeded', 0, ?)
                """,
                (
                    "3" * 64,
                    "4" * 64,
                    row["content_hash"],
                    "5" * 64,
                    "6" * 64,
                    "7" * 64,
                    "8" * 64,
                    utc_now(),
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO ai_reports(
                    report_key, period, timezone, local_date, period_start,
                    period_end, target_language, article_ids_json,
                    article_content_hash, artifact_id, created_at
                ) VALUES (?, 'daily', 'America/Los_Angeles', '2026-07-30',
                    '2026-07-30T07:00:00Z', '2026-07-31T06:59:59Z', 'en',
                    ?, ?, ?, ?)
                """,
                (
                    "9" * 64,
                    json.dumps([article_id]),
                    row["content_hash"],
                    int(digest_id),
                    utc_now(),
                ),
            )

        result = import_crawler_state(target, [source()], self.bundle)
        self.assertEqual(0, result["ai_artifacts_touched"])
        refreshed = target.article(article_id)
        self.assertIsNotNone(refreshed["read_at"])
        self.assertIsNotNone(refreshed["starred_at"])
        with target.connect() as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM ai_artifacts").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM ai_reports").fetchone()[0])

    def test_changed_metadata_updates_in_place_and_invalidates_old_ai_cache(self) -> None:
        target = self.database("changed.sqlite3")
        self.commit(target, [article("one", "Old title")])
        row = target.list_articles()[0]
        article_id = int(row["id"])
        with target.connect() as connection:
            connection.execute(
                "UPDATE articles SET updated_at='2026-01-01T00:00:00Z' WHERE id=?",
                (article_id,),
            )
            connection.execute(
                """
                INSERT INTO ai_artifacts(
                    article_id, task_type, input_scope, source_language,
                    target_language, artifact_key, input_hash,
                    article_content_hash, prompt_version, prompt_hash,
                    response_schema_version, response_schema_hash, provider,
                    requested_model, resolved_model, generation_params_hash,
                    output_json, output_text, output_hash, status,
                    input_truncated, created_at
                ) VALUES (?, 'summary', 'metadata', 'en', 'zh-CN', ?, ?, ?,
                    'v1', ?, 'v1', ?, 'subscription', 'model', 'model', ?,
                    '{}', 'summary', ?, 'succeeded', 0, ?)
                """,
                (
                    article_id,
                    "a" * 64,
                    "b" * 64,
                    row["content_hash"],
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    utc_now(),
                ),
            )

        result = import_crawler_state(target, [source()], self.bundle)
        self.assertEqual(1, result["updated"])
        refreshed = target.article(article_id)
        self.assertEqual("Article", refreshed["title"])
        self.assertEqual(article_id, int(refreshed["id"]))
        self.assertEqual({}, target.latest_ai_artifacts([article_id]))
        with target.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM ai_artifacts").fetchone()[0])

    def test_new_merge_article_is_unread_and_repeat_is_idempotent(self) -> None:
        target = self.database("new.sqlite3")
        first = import_crawler_state(target, [source()], self.bundle)
        self.assertEqual(1, first["inserted"])
        self.assertEqual(1, len(target.list_articles(unread_only=True)))
        second = import_crawler_state(target, [source()], self.bundle)
        self.assertEqual(0, second["inserted"])
        self.assertEqual(0, second["updated"])
        self.assertEqual(1, second["unchanged"])
        self.assertEqual(1, len(target.list_articles(unread_only=True)))

    def test_tampering_duplicate_keys_and_extra_fields_are_rejected_atomically(self) -> None:
        target = self.database("invalid.sqlite3")
        payload = self.payload()
        payload["articles"][0]["title"] = "Tampered"
        tampered = self.write_payload(payload, "tampered.json")
        with self.assertRaisesRegex(ValueError, "bundle hash"):
            import_crawler_state(target, [source()], tampered)
        self.assertEqual([], target.list_articles())

        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"protocol":"one","protocol":"two"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            import_crawler_state(target, [source()], duplicate)

        payload = self.payload()
        payload["secret"] = "not allowed"
        payload["bundle_hash"] = _bundle_hash(payload)
        extra = self.write_payload(payload, "extra.json")
        with self.assertRaisesRegex(ValueError, "top-level fields"):
            import_crawler_state(target, [source()], extra)

    def test_recomputed_bundle_hash_cannot_hide_invalid_article_content_hash(self) -> None:
        payload = copy.deepcopy(self.payload())
        payload["articles"][0]["title"] = "Different metadata"
        payload["bundle_hash"] = _bundle_hash(payload)
        invalid = self.write_payload(payload, "invalid-content-hash.json")
        target = self.database("hash.sqlite3")
        with self.assertRaisesRegex(ValueError, "content hash"):
            import_crawler_state(target, [source()], invalid)

    def test_source_identity_must_match_current_configuration(self) -> None:
        payload = self.payload()
        payload["sources"][0]["fetch_url"] = "https://evil.example/feed"
        payload["bundle_hash"] = _bundle_hash(payload)
        invalid = self.write_payload(payload, "identity.json")
        target = self.database("identity.sqlite3")
        with self.assertRaisesRegex(ValueError, "identity"):
            import_crawler_state(target, [source()], invalid)

    def test_fetchable_urls_are_bound_to_the_configured_publisher(self) -> None:
        original = self.payload()
        cases = []

        article_payload = copy.deepcopy(original)
        article_payload["articles"][0]["canonical_url"] = "https://evil.example/blog/one"
        row = article_payload["articles"][0]
        row["content_hash"] = stable_hash(
            row["canonical_url"],
            row["title"],
            row["summary"],
            row["author"],
            row["category"],
            row["published_at"],
            row["modified_at"],
        )
        article_payload["bundle_hash"] = _bundle_hash(article_payload)
        cases.append((article_payload, "article URL"))

        seen_payload = copy.deepcopy(original)
        seen_payload["seen_urls"][0]["url"] = "https://evil.example/blog/one"
        seen_payload["bundle_hash"] = _bundle_hash(seen_payload)
        cases.append((seen_payload, "seen URL"))

        pending_payload = copy.deepcopy(original)
        pending_payload["pending_urls"] = [
            {
                "source_slug": "example",
                "url": "https://evil.example/blog/new",
                "remote_modified": "2026-08-02",
                "change_kind": "new",
                "first_seen_at": "2026-08-02T00:00:00Z",
                "last_attempt_at": None,
                "next_attempt_at": None,
                "attempt_count": 0,
            }
        ]
        pending_payload["bundle_hash"] = _bundle_hash(pending_payload)
        cases.append((pending_payload, "pending URL"))

        for index, (payload, error) in enumerate(cases):
            with self.subTest(error=error):
                target = self.database("publisher-%s.sqlite3" % index)
                invalid = self.write_payload(payload, "publisher-%s.json" % index)
                with self.assertRaisesRegex(ValueError, error):
                    import_crawler_state(target, [source()], invalid)
                self.assertEqual([], target.list_articles())
                with target.connect() as connection:
                    self.assertEqual(
                        0,
                        connection.execute("SELECT COUNT(*) FROM seen_urls").fetchone()[0],
                    )


if __name__ == "__main__":
    unittest.main()
