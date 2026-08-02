import copy
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

import aaron_reader.ai_cache as ai_cache_module  # noqa: E402
from aaron_reader.ai_cache import (  # noqa: E402
    AI_CACHE_PROTOCOL,
    _cache_hash,
    _db_artifact,
    _entry_hash,
    _insert_artifact,
    _read_payload,
    _usage_day_window,
    _validate_payload,
    export_ai_cache,
    import_ai_cache,
)
from aaron_reader.ai_prompts import (  # noqa: E402
    canonical_json,
    parse_and_validate_output,
    stable_hash,
)
from aaron_reader.ai_service import AIGenerationHeld, AIService  # noqa: E402
from aaron_reader.ai_subscription import (  # noqa: E402
    export_subscription_batch,
    export_subscription_report,
    import_subscription_report,
    import_subscription_results,
)
from aaron_reader.cli import build_parser  # noqa: E402
from aaron_reader.config import load_config  # noqa: E402
from aaron_reader.database import AIBudgetExceeded, Database, utc_now  # noqa: E402
from aaron_reader.models import (  # noqa: E402
    AIConfig,
    AppConfig,
    ArticleCandidate,
    SourceConfig,
)
from aaron_reader.render import render_index, render_outputs  # noqa: E402


class PublicAICacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = SourceConfig(
            slug="example",
            name="Example",
            home_url="https://example.com/blog",
            fetch_url="https://example.com/feed.xml",
            adapter="rss",
        )
        self.old_config = AppConfig(
            sources=[self.source],
            ai=AIConfig(
                enabled=False,
                provider="openai",
                summary_model="gpt-5.6-luna",
                translation_model="gpt-5.6-luna",
                digest_model="gpt-5.6-luna",
                reasoning_effort="medium",
                api_key_environment="OPENAI_API_KEY",
            ),
        )
        self.new_config = AppConfig(
            sources=[self.source],
            ai=AIConfig(
                enabled=False,
                provider="deepseek",
                summary_model="deepseek-v4-flash",
                translation_model="deepseek-v4-flash",
                digest_model="deepseek-v4-flash",
                reasoning_effort="none",
                api_key_environment="DEEPSEEK_API_KEY",
            ),
        )
        self.candidates = [
            self.candidate("monday", "2026-07-27T12:00:00Z"),
            self.candidate("friday", "2026-07-31T12:00:00Z"),
            self.candidate("today", "2026-08-01T08:00:00Z"),
        ]
        self.source_database = self.database("source.sqlite3", self.candidates)
        self.populate_source_ai()
        self.bundle = self.root / "ai-cache.json"
        self.export_result = export_ai_cache(
            self.source_database, self.old_config, self.bundle
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def candidate(self, slug: str, published_at: str) -> ArticleCandidate:
        url = "https://example.com/blog/%s" % slug
        title = "Title %s" % slug
        summary = "Publisher summary %s" % slug
        return ArticleCandidate(
            source_slug=self.source.slug,
            external_id="external-%s" % slug,
            url=url,
            title=title,
            summary=summary,
            author="Publisher",
            category="Research",
            published_at=published_at,
            content_hash=stable_hash(
                canonical_json(
                    {
                        "url": url,
                        "title": title,
                        "summary": summary,
                        "published_at": published_at,
                    }
                )
            ),
        )

    def database(
        self,
        name: str,
        candidates=None,
        *,
        reverse: bool = False,
        shift_ids: bool = False,
    ) -> Database:
        database = Database(self.root / name)
        database.initialize()
        database.sync_source_configs([self.source])
        if shift_ids:
            dummy = self.candidate("temporary-id-shift", "2026-01-01T00:00:00Z")
            database.commit_candidates(
                self.source,
                [dummy],
                started_at="2026-01-01T00:00:00Z",
                http_status=200,
                etag="",
                last_modified="",
                body_hash="dummy",
            )
            with database.connect() as connection:
                connection.execute("DELETE FROM articles")
        if candidates:
            values = list(reversed(candidates)) if reverse else list(candidates)
            database.commit_candidates(
                self.source,
                values,
                started_at="2026-08-01T17:00:00Z",
                http_status=200,
                etag="",
                last_modified="",
                body_hash="fixture",
            )
        return database

    def populate_source_ai(self) -> None:
        service = AIService(self.old_config, self.source_database)
        articles = self.source_database.list_articles(limit=100)
        request = export_subscription_batch(
            service,
            articles,
            target_language="zh-CN",
            limit=50,
        )
        items = []
        for request_item in request["items"]:
            article_id = int(request_item["article_id"])
            article = self.source_database.article(article_id)
            items.append(
                {
                    "article_id": article_id,
                    "fingerprint": request_item["fingerprint"],
                    "summary": {
                        "summary": "摘要：%s" % article["title"],
                        "key_points": ["严格依据发布方元数据"],
                        "language": "zh-CN",
                        "basis": "metadata",
                        "limitations": "没有读取全文。",
                    },
                    "translation": {
                        "title": "译文：%s" % article["title"],
                        "publisher_summary": "译文：%s" % article["summary"],
                        "language": "zh-CN",
                        "limitations": "",
                    },
                }
            )
        import_subscription_results(
            service,
            {
                "protocol": request["protocol"],
                "batch_id": request["batch_id"],
                "target_language": request["target_language"],
                "items": items,
            },
        )

        now = datetime(2026, 8, 1, 17, 0, tzinfo=timezone.utc)
        for period in ("daily", "weekly"):
            report_request = export_subscription_report(
                service,
                period=period,
                target_language="zh-CN",
                now=now,
            )
            report_articles = report_request["input"]["articles"]
            payload = {
                key: report_request[key]
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
            payload["output"] = {
                "headline": "%s 云端简报" % period,
                "overview": "这是可公开复用的 AI 报告。",
                "items": [
                    {
                        "article_id": int(article["article_id"]),
                        "title": "报告：%s" % article["title"],
                        "summary": "该文章的严格元数据摘要。",
                    }
                    for article in report_articles
                ],
                "language": "zh-CN",
                "limitations": "没有读取全文。",
            }
            import_subscription_report(service, payload)

    def payload(self):
        return json.loads(self.bundle.read_text(encoding="utf-8"))

    def write_payload(self, payload, name="modified.json"):
        path = self.root / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def rehash(payload):
        payload["bundle_hash"] = _cache_hash(payload)
        return payload

    def target_database(self, name="target.sqlite3"):
        return self.database(
            name,
            self.candidates,
            reverse=True,
            shift_ids=True,
        )

    def reserve_usage_attempt(
        self,
        database: Database,
        suffix: str,
        *,
        reserved_cost_micros: int = 0,
    ):
        job = database.ensure_ai_job(
            artifact_key=stable_hash("usage-%s" % suffix),
            article_id=None,
            task_type="digest",
            input_scope="digest",
            target_language="zh-CN",
            request={"version": 1, "usage_fixture": suffix},
            trigger_kind="test",
            max_attempts=1,
        )
        return database.reserve_ai_attempt(
            job_id=int(job["id"]),
            idempotency_key="private-idempotency-%s" % suffix,
            requested_model="private-model-%s" % suffix,
            estimated_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_micros=reserved_cost_micros,
            price_snapshot={},
            daily_started_at="2000-01-01T08:00:00Z",
            monthly_started_at="2000-01-01T08:00:00Z",
            daily_reset_at="2100-01-02T08:00:00Z",
            monthly_reset_at="2100-02-01T08:00:00Z",
            daily_max_requests=1_000,
            daily_max_total_tokens=1_000_000,
            daily_max_cost_micros=1_000_000,
            monthly_max_requests=1_000,
            monthly_max_total_tokens=1_000_000,
            monthly_max_cost_micros=1_000_000,
        )

    @staticmethod
    def carried_usage_entry(requests=1):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        day_start, day_end = _usage_day_window(now)
        return {
            "timezone": "America/Los_Angeles",
            "day_start": day_start,
            "day_end": day_end,
            "covered_through": now.isoformat().replace("+00:00", "Z"),
            "requests": requests,
            "confirmed_requests": requests,
            "unconfirmed_requests": 0,
            "input_tokens": 10 * requests,
            "cached_input_tokens": 0,
            "cache_miss_input_tokens": 10 * requests,
            "cache_write_input_tokens": 0,
            "output_tokens": 5 * requests,
            "reasoning_tokens": 0,
            "total_tokens": 15 * requests,
            "reserved_total_tokens_for_unconfirmed": 0,
            "cost_micros": 0,
            "reserved_cost_micros_for_unconfirmed": 0,
        }

    @staticmethod
    def generation_hold_entry(
        database,
        config,
        canonical_url,
        *,
        task_type="summary",
        hold_class="ambiguous",
    ):
        article = database.article_by_url("example", canonical_url)
        service = AIService(config, database)
        prepared = service.prepare_article(
            int(article["id"]),
            task_type=task_type,
            target_language="zh-CN",
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        template = service._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        now = utc_now()
        return {
            **template,
            "hold_class": hold_class,
            "first_seen_at": now,
            "last_seen_at": now,
        }

    def test_export_is_public_safe_versioned_and_hash_bound(self) -> None:
        payload = self.payload()
        self.assertEqual(AI_CACHE_PROTOCOL, payload["protocol"])
        self.assertEqual(_cache_hash(payload), payload["bundle_hash"])
        self.assertEqual(6, len(payload["artifacts"]))
        self.assertEqual(2, len(payload["reports"]))
        self.assertEqual(8, self.export_result["artifacts"])
        self.assertEqual(0, self.export_result["skipped_incompatible"])

        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(payload)
        self.assertFalse(
            keys
            & {
                "article_id",
                "artifact_id",
                "artifact_key",
                "report_key",
                "input_hash",
                "provider_response_id",
                "api_key",
                "request_json",
                "error_message",
                "read_at",
                "starred_at",
                "normalized_text",
                "content_snapshot_id",
            }
        )
        self.assertEqual(
            {"source_slug", "external_id", "canonical_url", "content_hash"},
            set(payload["artifacts"][0]["article"]),
        )

    def test_cross_provider_import_remaps_every_id_and_renders(self) -> None:
        target = self.target_database()
        source_by_url = {
            article["canonical_url"]: int(article["id"])
            for article in self.source_database.list_articles(limit=100)
        }
        target_by_url = {
            article["canonical_url"]: int(article["id"])
            for article in target.list_articles(limit=100)
        }
        self.assertTrue(
            all(source_by_url[url] != target_by_url[url] for url in source_by_url)
        )

        # Personal state is outside the cache and must survive the import.
        today = target.article_by_url("example", "https://example.com/blog/today")
        target.set_read([int(today["id"])], True)
        target.set_starred(int(today["id"]), True)

        first = import_ai_cache(target, self.new_config, self.bundle)
        second = import_ai_cache(target, self.new_config, self.bundle)
        self.assertEqual((8, 2), (first["inserted_artifacts"], first["inserted_reports"]))
        self.assertEqual((8, 2), (second["artifact_cache_hits"], second["report_cache_hits"]))
        self.assertFalse(first["api_key_used"])
        self.assertEqual(0, first["provider_api_calls"])

        with target.connect() as connection:
            self.assertEqual(
                (8, 2, 0, 0, 0),
                tuple(
                    connection.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM ai_artifacts),
                            (SELECT COUNT(*) FROM ai_reports),
                            (SELECT COUNT(*) FROM ai_attempts),
                            (SELECT COUNT(*) FROM ai_jobs),
                            (SELECT COUNT(*) FROM article_content_snapshots)
                        """
                    ).fetchone()
                ),
            )
            requested_models = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT requested_model FROM ai_artifacts"
                ).fetchall()
            }
            self.assertEqual({"gpt-5.6-luna"}, requested_models)
            report_ids = json.loads(
                connection.execute(
                    "SELECT article_ids_json FROM ai_reports WHERE period='weekly'"
                ).fetchone()[0]
            )
            weekly_public = next(
                report for report in self.payload()["reports"]
                if report["period"] == "weekly"
            )
            expected_target_ids = [
                target_by_url[identity["canonical_url"]]
                for identity in weekly_public["articles"]
            ]
            expected_source_ids = [
                source_by_url[identity["canonical_url"]]
                for identity in weekly_public["articles"]
            ]
            self.assertEqual(expected_target_ids, report_ids)
            self.assertNotEqual(expected_source_ids, report_ids)

        refreshed = target.article(int(today["id"]))
        self.assertIsNotNone(refreshed["read_at"])
        self.assertIsNotNone(refreshed["starred_at"])
        html = render_index(target, language="en")
        self.assertIn("译文：Title today", html)
        rendered = self.root / "rendered-target"
        render_outputs(target, rendered, language="en")
        latest = json.loads((rendered / "latest.json").read_text(encoding="utf-8"))
        weekly_rendered = next(
            report for report in latest["ai_reports"]
            if report["period"] == "weekly"
        )
        self.assertEqual("weekly 云端简报", weekly_rendered["output"]["headline"])
        self.assertEqual(
            expected_target_ids,
            [item["article_id"] for item in weekly_rendered["output"]["items"]],
        )

        round_trip = self.root / "round-trip.json"
        export_ai_cache(target, self.new_config, round_trip)
        round_trip_payload = json.loads(round_trip.read_text(encoding="utf-8"))
        self.assertEqual(self.payload()["artifacts"], round_trip_payload["artifacts"])
        self.assertEqual(self.payload()["reports"], round_trip_payload["reports"])

    def test_usage_ledger_is_aggregate_durable_idempotent_and_budget_enforced(self) -> None:
        confirmed = self.reserve_usage_attempt(
            self.source_database,
            "confirmed",
            reserved_cost_micros=50,
        )
        self.reserve_usage_attempt(
            self.source_database,
            "unconfirmed",
            reserved_cost_micros=77,
        )
        with self.source_database.connect() as connection:
            connection.execute(
                """
                UPDATE ai_attempts SET state='succeeded', reservation_active=0,
                    actual_input_tokens=100, actual_cached_input_tokens=20,
                    actual_cache_write_tokens=0, actual_output_tokens=40,
                    actual_reasoning_tokens=5, actual_total_tokens=140,
                    actual_cost_micros=123
                WHERE id=?
                """,
                (int(confirmed["id"]),),
            )

        usage_bundle = self.root / "usage-cache.json"
        exported = export_ai_cache(
            self.source_database, self.old_config, usage_bundle
        )
        payload = json.loads(usage_bundle.read_text(encoding="utf-8"))
        self.assertEqual((1, 2), (exported["usage_days"], exported["usage_requests"]))
        entry = payload["usage_ledger"][0]
        self.assertEqual(
            {
                "requests": 2,
                "confirmed_requests": 1,
                "unconfirmed_requests": 1,
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "cache_miss_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 40,
                "reasoning_tokens": 5,
                "total_tokens": 140,
                "reserved_total_tokens_for_unconfirmed": 30,
                "cost_micros": 123,
                "reserved_cost_micros_for_unconfirmed": 77,
            },
            {key: entry[key] for key in (
                "requests",
                "confirmed_requests",
                "unconfirmed_requests",
                "input_tokens",
                "cached_input_tokens",
                "cache_miss_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
                "reserved_total_tokens_for_unconfirmed",
                "cost_micros",
                "reserved_cost_micros_for_unconfirmed",
            )},
        )
        serialized = usage_bundle.read_text(encoding="utf-8")
        for forbidden in (
            "idempotency",
            "private-model",
            "provider_request_id",
            "error_message",
            "request_json",
            "attempt_id",
        ):
            self.assertNotIn(forbidden, serialized)

        target = self.target_database("usage-target.sqlite3")
        first = import_ai_cache(target, self.new_config, usage_bundle)
        second = import_ai_cache(target, self.new_config, usage_bundle)
        self.assertEqual((1, 2), (first["usage_days"], first["usage_requests"]))
        self.assertEqual(1, len(target.list_ai_usage_ledger()))
        status = target.ai_status(entry["day_start"], entry["day_start"])
        self.assertEqual(
            {"requests": 2, "total_tokens": 170, "cost_micros": 200},
            status["daily"],
        )
        self.assertEqual(status["daily"], target.ai_status(
            entry["day_start"], entry["day_start"]
        )["daily"])

        job = target.ensure_ai_job(
            artifact_key=stable_hash("budget-must-carry"),
            article_id=None,
            task_type="digest",
            input_scope="digest",
            target_language="zh-CN",
            request={"version": 1},
            trigger_kind="test",
            max_attempts=1,
        )
        with self.assertRaisesRegex(AIBudgetExceeded, "daily request budget exhausted"):
            target.reserve_ai_attempt(
                job_id=int(job["id"]),
                idempotency_key="must-not-be-stored",
                requested_model="must-not-run",
                estimated_input_tokens=1,
                reserved_output_tokens=1,
                reserved_cost_micros=0,
                price_snapshot={},
                daily_started_at=entry["day_start"],
                monthly_started_at=entry["day_start"],
                daily_reset_at=entry["day_end"],
                monthly_reset_at=entry["day_end"],
                daily_max_requests=2,
                daily_max_total_tokens=10_000,
                daily_max_cost_micros=0,
                monthly_max_requests=2,
                monthly_max_total_tokens=10_000,
                monthly_max_cost_micros=0,
            )
        self.assertEqual(0, len(target.list_ai_attempts()))

    def test_generation_holds_are_public_durable_and_stable_across_local_ids(self) -> None:
        holds = [
            self.generation_hold_entry(
                self.source_database,
                self.new_config,
                "https://example.com/blog/today",
                hold_class="ambiguous",
            ),
            self.generation_hold_entry(
                self.source_database,
                self.new_config,
                "https://example.com/blog/friday",
                task_type="translation",
                hold_class="paid_failure",
            ),
        ]
        self.source_database.replace_ai_generation_holds(holds)
        hold_bundle = self.root / "generation-holds.json"
        exported = export_ai_cache(
            self.source_database,
            self.old_config,
            hold_bundle,
        )
        payload = json.loads(hold_bundle.read_text(encoding="utf-8"))
        self.assertEqual(
            (2, 1, 1),
            (
                exported["generation_holds"],
                exported["ambiguous_holds"],
                exported["paid_failure_holds"],
            ),
        )
        self.assertEqual(0, exported["skipped_generation_holds"])

        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(payload["generation_holds"])
        self.assertFalse(
            keys
            & {
                "article_id",
                "portable_input",
                "request_id",
                "provider_request_id",
                "request_body",
                "response_body",
                "error",
                "error_code",
                "error_message",
                "api_key",
                "authorization",
            }
        )

        target = self.target_database("generation-hold-target.sqlite3")
        imported = import_ai_cache(target, self.new_config, hold_bundle)
        self.assertEqual(
            (2, 1, 1),
            (
                imported["generation_holds"],
                imported["ambiguous_holds"],
                imported["paid_failure_holds"],
            ),
        )
        self.assertEqual(payload["generation_holds"], target.list_ai_generation_holds())

        for source_hold in holds:
            identity = source_hold["descriptor"]["article_identities"][0]
            target_article = target.article_by_url(
                identity["source_slug"], identity["canonical_url"]
            )
            target_prepared = AIService(self.new_config, target).prepare_article(
                int(target_article["id"]),
                task_type=source_hold["descriptor"]["task_type"],
                target_language="zh-CN",
                input_scope="metadata",
                translated_fields=("title", "publisher_summary"),
            )
            target_template = AIService(
                self.new_config, target
            )._generation_hold_template(
                target_prepared,
                workload_kind="article",
            )
            self.assertEqual(source_hold["hold_key"], target_template["hold_key"])
            with self.assertRaises(AIGenerationHeld):
                AIService(self.new_config, target)._check_generation_hold(
                    target_template,
                    force_held=False,
                )

        round_trip = self.root / "generation-holds-round-trip.json"
        export_ai_cache(target, self.new_config, round_trip)
        round_trip_payload = json.loads(round_trip.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["generation_holds"],
            round_trip_payload["generation_holds"],
        )

    def test_generation_hold_descriptor_and_hash_tampering_are_rejected(self) -> None:
        hold = self.generation_hold_entry(
            self.source_database,
            self.new_config,
            "https://example.com/blog/today",
        )
        self.source_database.replace_ai_generation_holds([hold])
        hold_bundle = self.root / "generation-hold-tamper-source.json"
        export_ai_cache(self.source_database, self.old_config, hold_bundle)
        original = json.loads(hold_bundle.read_text(encoding="utf-8"))
        target = self.target_database("generation-hold-tamper-target.sqlite3")

        for field, value in (("article_id", 7), ("error_message", "private")):
            payload = copy.deepcopy(original)
            descriptor = payload["generation_holds"][0]["descriptor"]
            descriptor[field] = value
            payload["generation_holds"][0]["hold_key"] = stable_hash(descriptor)
            self.rehash(payload)
            with self.assertRaisesRegex(ValueError, "descriptor fields"):
                import_ai_cache(
                    target,
                    self.new_config,
                    self.write_payload(payload, "hold-%s.json" % field),
                )

        payload = copy.deepcopy(original)
        payload["generation_holds"][0]["descriptor"]["portable_input_hash"] = "f" * 64
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "key does not match descriptor"):
            import_ai_cache(
                target,
                self.new_config,
                self.write_payload(payload, "hold-hash.json"),
            )

    def test_tampering_duplicate_keys_extra_fields_and_nonfinite_json_fail(self) -> None:
        target = self.target_database()
        payload = self.payload()
        payload["artifacts"][0]["output"]["summary"] = "tampered"
        tampered = self.write_payload(payload, "tampered.json")
        with self.assertRaisesRegex(ValueError, "bundle hash"):
            import_ai_cache(target, self.new_config, tampered)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"protocol":"one","protocol":"two"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            import_ai_cache(target, self.new_config, duplicate)

        nonfinite = self.root / "nonfinite.json"
        nonfinite.write_text('{"number":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "non-finite"):
            import_ai_cache(target, self.new_config, nonfinite)

        payload = self.payload()
        payload["api_key"] = "must-never-be-accepted"
        self.rehash(payload)
        extra = self.write_payload(payload, "extra.json")
        with self.assertRaisesRegex(ValueError, "top-level fields"):
            import_ai_cache(target, self.new_config, extra)

        with target.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM ai_artifacts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM ai_reports").fetchone()[0])

    def test_rehashed_identity_output_and_entry_tampering_are_rejected(self) -> None:
        target = self.target_database()

        payload = self.payload()
        entry = payload["artifacts"][0]
        entry["article"]["content_hash"] = "f" * 64
        entry["cache_key"] = _entry_hash(entry)
        payload["artifacts"].sort(
            key=lambda item: (
                (
                    item["article"]["source_slug"],
                    item["article"]["external_id"],
                    item["article"]["canonical_url"],
                    item["article"]["content_hash"],
                ),
                item["task_type"],
                item["target_language"],
                item["cache_key"],
            )
        )
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "content_hash differs"):
            import_ai_cache(
                target, self.new_config, self.write_payload(payload, "identity.json")
            )

        payload = self.payload()
        entry = payload["artifacts"][0]
        entry["output_hash"] = "e" * 64
        entry["cache_key"] = _entry_hash(entry)
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "output_hash"):
            import_ai_cache(
                target, self.new_config, self.write_payload(payload, "output.json")
            )

        payload = self.payload()
        entry = payload["artifacts"][0]
        entry["requested_model"] = "forged-model"
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "cache_key"):
            import_ai_cache(
                target, self.new_config, self.write_payload(payload, "entry.json")
            )

    def test_prompt_schema_and_report_identity_contracts_are_strict(self) -> None:
        target = self.target_database()
        payload = self.payload()
        entry = payload["artifacts"][0]
        entry["prompt_hash"] = "a" * 64
        entry["cache_key"] = _entry_hash(entry)
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "current prompt or schema"):
            import_ai_cache(
                target, self.new_config, self.write_payload(payload, "prompt.json")
            )

        payload = self.payload()
        report = next(
            report for report in payload["reports"]
            if len(report["articles"]) > 1
        )
        report["artifact"]["output"]["items"][0]["article"] = copy.deepcopy(
            report["articles"][-1]
        )
        report["artifact"]["output_hash"] = stable_hash(
            canonical_json(report["artifact"]["output"])
        )
        report["cache_key"] = _entry_hash(report)
        self.rehash(payload)
        with self.assertRaisesRegex(ValueError, "item identities"):
            import_ai_cache(
                target, self.new_config, self.write_payload(payload, "report.json")
            )

    def test_collision_after_an_insert_rolls_back_the_whole_transaction(self) -> None:
        target = self.target_database()
        original_ledger = [self.carried_usage_entry()]
        target.replace_ai_usage_ledger(original_ledger)
        original_holds = [
            self.generation_hold_entry(
                target,
                self.new_config,
                "https://example.com/blog/today",
            )
        ]
        target.replace_ai_generation_holds(original_holds)
        payload = self.payload()
        collision_entry = payload["artifacts"][1]
        article = target.article_by_url(
            "example", collision_entry["article"]["canonical_url"]
        )
        prepared = AIService(self.new_config, target).prepare_article(
            int(article["id"]),
            task_type=collision_entry["task_type"],
            target_language=collision_entry["target_language"],
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        wrong_output = copy.deepcopy(collision_entry["output"])
        if collision_entry["task_type"] == "summary":
            wrong_output["summary"] = "different but valid output"
        else:
            wrong_output["title"] = "different but valid translation"
        validated, readable = parse_and_validate_output(
            collision_entry["task_type"],
            canonical_json(wrong_output),
            target_language=collision_entry["target_language"],
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        conflicting = _db_artifact(
            prepared, collision_entry, validated, readable
        )
        with target.connect() as connection:
            _insert_artifact(connection, conflicting)

        with self.assertRaisesRegex(ValueError, "artifact key collision"):
            import_ai_cache(target, self.new_config, self.bundle)
        with target.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM ai_artifacts").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM ai_reports").fetchone()[0])
        self.assertEqual(original_ledger, target.list_ai_usage_ledger())
        self.assertEqual(original_holds, target.list_ai_generation_holds())

    def test_article_change_omits_its_artifacts_and_every_affected_report(self) -> None:
        self.source_database.replace_ai_generation_holds(
            [
                self.generation_hold_entry(
                    self.source_database,
                    self.new_config,
                    "https://example.com/blog/today",
                )
            ]
        )
        today = self.source_database.article_by_url(
            "example", "https://example.com/blog/today"
        )
        with self.source_database.connect() as connection:
            connection.execute(
                "UPDATE articles SET content_hash=? WHERE id=?",
                ("f" * 64, int(today["id"])),
            )
        stale_path = self.root / "stale.json"
        result = export_ai_cache(
            self.source_database, self.old_config, stale_path
        )
        payload = json.loads(stale_path.read_text(encoding="utf-8"))
        self.assertEqual(4, result["article_artifacts"])
        self.assertEqual(0, result["reports"])
        self.assertEqual(0, result["generation_holds"])
        self.assertEqual(1, result["skipped_generation_holds"])
        self.assertEqual([], payload["generation_holds"])
        self.assertTrue(
            all(
                entry["article"]["canonical_url"]
                != "https://example.com/blog/today"
                for entry in payload["artifacts"]
            )
        )

    def test_export_retains_only_latest_report_per_period_and_language(self) -> None:
        service = AIService(self.old_config, self.source_database)
        # A newly published article gives the later fixed window a distinct,
        # still-valid report while the earlier fixed window remains valid too.
        self.source_database.commit_candidates(
            self.source,
            [self.candidate("late-today", "2026-08-01T17:30:00Z")],
            started_at="2026-08-01T18:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="later-report-fixture",
        )
        later = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)
        request = export_subscription_report(
            service,
            period="daily",
            target_language="zh-CN",
            now=later,
        )
        payload = {
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
        payload["output"] = {
            "headline": "newest daily report",
            "overview": "Only this daily report should remain in the handoff.",
            "items": [
                {
                    "article_id": int(article["article_id"]),
                    "title": "Latest: %s" % article["title"],
                    "summary": "Latest bounded report item.",
                }
                for article in request["input"]["articles"]
            ],
            "language": "zh-CN",
            "limitations": "Metadata only.",
        }
        import_subscription_report(service, payload)
        with self.source_database.connect() as connection:
            self.assertEqual(3, connection.execute("SELECT COUNT(*) FROM ai_reports").fetchone()[0])

        latest_path = self.root / "latest-reports.json"
        result = export_ai_cache(
            self.source_database, self.old_config, latest_path
        )
        handoff = json.loads(latest_path.read_text(encoding="utf-8"))
        self.assertEqual(2, result["reports"])
        daily = next(report for report in handoff["reports"] if report["period"] == "daily")
        self.assertEqual("2026-08-01T18:00:00Z", daily["period_end"])
        self.assertEqual(
            "newest daily report", daily["artifact"]["output"]["headline"]
        )

    def test_size_limit_symlinks_and_cli_syntax(self) -> None:
        target = self.target_database()
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"{" + b" " * 64 + b"}")
        with mock.patch.object(ai_cache_module, "AI_CACHE_MAX_BYTES", 32):
            with self.assertRaisesRegex(ValueError, "size"):
                import_ai_cache(target, self.new_config, oversized)

        symlink = self.root / "symlink.json"
        symlink.symlink_to(self.bundle)
        with self.assertRaisesRegex(ValueError, "regular file"):
            import_ai_cache(target, self.new_config, symlink)

        imported = build_parser().parse_args(
            ["ai-cache-import", "cloud/ai-cache.json", "--json"]
        )
        exported = build_parser().parse_args(
            ["ai-cache-export", "cloud/ai-cache.json", "--json"]
        )
        self.assertEqual("ai-cache-import", imported.command)
        self.assertEqual("ai-cache-export", exported.command)
        self.assertTrue(imported.json and exported.json)


class TrackedPublicAICacheTests(unittest.TestCase):
    def test_tracked_cache_is_strict_nonempty_unique_and_has_no_local_ids(self) -> None:
        path = REPOSITORY_ROOT / "cloud" / "ai-cache.json"
        payload = _read_payload(path)
        config = load_config(str(REPOSITORY_ROOT / "config" / "sources.json"))
        _validate_payload(payload, config.sources, verify_hash=True)
        # Production state evolves every cycle.  Test structural coverage and
        # uniqueness, never one historical snapshot's exact count or hash.
        self.assertTrue(payload["artifacts"])
        self.assertEqual(
            {"summary", "translation"},
            {entry["task_type"] for entry in payload["artifacts"]},
        )
        bindings = {
            (
                entry["article"]["source_slug"],
                entry["article"]["external_id"],
                entry["article"]["canonical_url"],
                entry["article"]["content_hash"],
                entry["task_type"],
                entry["target_language"],
            )
            for entry in payload["artifacts"]
        }
        self.assertEqual(len(payload["artifacts"]), len(bindings))
        report_bindings = {
            (report["period"], report["target_language"])
            for report in payload["reports"]
        }
        self.assertEqual(len(payload["reports"]), len(report_bindings))

        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(payload)
        self.assertNotIn("article_id", keys)
        self.assertNotIn("provider_response_id", keys)
        self.assertNotIn("read_at", keys)
        self.assertNotIn("starred_at", keys)


if __name__ == "__main__":
    unittest.main()
