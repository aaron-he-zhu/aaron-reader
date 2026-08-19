import copy
import json
import sys
import tempfile
from dataclasses import replace
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
from aaron_reader.ai_provider import (  # noqa: E402
    DEEPSEEK_MODEL,
    OPENROUTER_MODEL,
    ProviderHTTPError,
    ProviderResponse,
    ProviderUsage,
)
from aaron_reader.ai_service import (  # noqa: E402
    AIFallbackEligibleError,
    AIGenerationHeld,
    AIService,
    AIServiceError,
)
from aaron_reader.ai_subscription import (  # noqa: E402
    export_subscription_batch,
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
        article = database.article_by_url(
            self.source.slug,
            "https://example.com/blog/today",
        )
        job = database.ensure_ai_job(
            artifact_key=stable_hash("usage-%s" % suffix),
            article_id=int(article["id"]),
            task_type="translation",
            input_scope="metadata",
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
        self.assertEqual("aaron-reader-public-ai-cache-v3", AI_CACHE_PROTOCOL)
        self.assertEqual(AI_CACHE_PROTOCOL, payload["protocol"])
        self.assertEqual(
            {
                "protocol",
                "exported_at",
                "bundle_hash",
                "artifacts",
                "usage_ledger",
                "generation_holds",
            },
            set(payload),
        )
        self.assertNotIn("reports", payload)
        self.assertEqual(_cache_hash(payload), payload["bundle_hash"])
        self.assertEqual(6, len(payload["artifacts"]))
        self.assertEqual(AI_CACHE_PROTOCOL, self.export_result["protocol"])
        self.assertEqual(6, self.export_result["article_artifacts"])
        self.assertEqual(6, self.export_result["artifacts"])
        self.assertEqual(0, self.export_result["skipped_incompatible"])
        self.assertFalse(
            {key for key in self.export_result if "report" in key}
        )

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
        self.assertEqual(6, first["inserted_artifacts"])
        self.assertEqual(6, second["artifact_cache_hits"])
        for result in (first, second):
            self.assertEqual(AI_CACHE_PROTOCOL, result["protocol"])
            self.assertFalse({key for key in result if "report" in key})
        self.assertFalse(first["api_key_used"])
        self.assertEqual(0, first["provider_api_calls"])

        with target.connect() as connection:
            self.assertEqual(
                (6, 0, 0, 0),
                tuple(
                    connection.execute(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM ai_artifacts),
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

        refreshed = target.article(int(today["id"]))
        self.assertIsNotNone(refreshed["read_at"])
        self.assertIsNotNone(refreshed["starred_at"])
        html = render_index(target, language="en")
        self.assertIn("译文：Title today", html)
        rendered = self.root / "rendered-target"
        render_outputs(target, rendered, language="en")
        latest = json.loads((rendered / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("ai_reports", latest)
        self.assertNotIn("cached_ai_report_count", latest)

        round_trip = self.root / "round-trip.json"
        round_trip_result = export_ai_cache(target, self.new_config, round_trip)
        round_trip_payload = json.loads(round_trip.read_text(encoding="utf-8"))
        self.assertEqual(self.payload()["artifacts"], round_trip_payload["artifacts"])
        self.assertNotIn("reports", round_trip_payload)
        self.assertFalse({key for key in round_trip_result if "report" in key})

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

        budget_article = target.article_by_url(
            self.source.slug,
            "https://example.com/blog/today",
        )
        job = target.ensure_ai_job(
            artifact_key=stable_hash("budget-must-carry"),
            article_id=int(budget_article["id"]),
            task_type="translation",
            input_scope="metadata",
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
            self.generation_hold_entry(
                self.source_database,
                self.new_config,
                "https://example.com/blog/monday",
                hold_class="fallback_pending",
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
            (3, 1, 1, 1),
            (
                exported["generation_holds"],
                exported["ambiguous_holds"],
                exported["paid_failure_holds"],
                exported["fallback_pending_holds"],
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
            (3, 1, 1, 1),
            (
                imported["generation_holds"],
                imported["ambiguous_holds"],
                imported["paid_failure_holds"],
                imported["fallback_pending_holds"],
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

    def test_legacy_article_pair_hold_round_trips_without_local_ids(self) -> None:
        pair_candidate = self.candidate(
            "pair-hold",
            "2026-08-01T09:00:00Z",
        )
        source = self.database("pair-hold-source.sqlite3", [pair_candidate])
        source_article = source.article_by_url(
            self.source.slug,
            "https://example.com/blog/pair-hold",
        )
        pair_config = replace(
            self.new_config,
            ai=replace(
                self.new_config.ai,
                enabled=True,
                fallback_provider="",
            ),
        )

        class InvalidPairProvider:
            def generate(self, request):
                return ProviderResponse(
                    output_text="{}",
                    usage=ProviderUsage(
                        input_tokens=20,
                        output_tokens=1,
                        total_tokens=21,
                    ),
                    model=DEEPSEEK_MODEL,
                    request_id="pair-hold-fixture",
                )

        service = AIService(
            pair_config,
            source,
            provider=InvalidPairProvider(),
        )
        with mock.patch.dict(
            "os.environ",
            {"DEEPSEEK_API_KEY": "test-key"},
            clear=True,
        ):
            with self.assertRaises(AIServiceError):
                service.generate_article_pair(int(source_article["id"]))

        holds = source.list_ai_generation_holds()
        self.assertEqual(1, len(holds))
        self.assertEqual("article_pair", holds[0]["workload_kind"])
        self.assertEqual("paid_failure", holds[0]["hold_class"])
        self.assertNotIn("article_id", holds[0]["descriptor"])

        bundle = self.root / "pair-hold.json"
        exported = export_ai_cache(source, pair_config, bundle)
        payload = json.loads(bundle.read_text(encoding="utf-8"))
        self.assertEqual(1, exported["generation_holds"])
        self.assertEqual(holds, payload["generation_holds"])

        target = self.database(
            "pair-hold-target.sqlite3",
            [pair_candidate],
            reverse=True,
            shift_ids=True,
        )
        target_article = target.article_by_url(
            self.source.slug,
            "https://example.com/blog/pair-hold",
        )
        self.assertNotEqual(source_article["id"], target_article["id"])
        imported = import_ai_cache(target, pair_config, bundle)
        self.assertEqual(1, imported["generation_holds"])
        self.assertEqual(holds, target.list_ai_generation_holds())

        round_trip = self.root / "pair-hold-round-trip.json"
        export_ai_cache(target, pair_config, round_trip)
        round_trip_payload = json.loads(round_trip.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["generation_holds"],
            round_trip_payload["generation_holds"],
        )

    def test_hard_crash_hold_survives_public_cache_and_blocks_fresh_runner(self):
        class FatalProvider:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise KeyboardInterrupt("simulated runner termination")

        enabled_config = replace(
            self.new_config,
            ai=replace(self.new_config.ai, enabled=True),
        )
        crash_candidate = self.candidate(
            "hard-crash", "2026-08-01T09:00:00Z"
        )
        self.source_database.commit_candidates(
            self.source,
            [crash_candidate],
            started_at="2026-08-01T17:01:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="hard-crash-article",
        )
        source_article = self.source_database.article_by_url(
            self.source.slug,
            "https://example.com/blog/hard-crash",
        )
        fatal = FatalProvider()
        source_service = AIService(
            enabled_config,
            self.source_database,
            provider=fatal,
        )
        with mock.patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            with self.assertRaises(KeyboardInterrupt):
                source_service.generate_article(
                    int(source_article["id"]),
                    task_type="summary",
                )
        self.assertEqual(1, len(fatal.requests))
        self.assertEqual(
            "ambiguous",
            self.source_database.list_ai_generation_holds()[0]["hold_class"],
        )

        crash_bundle = self.root / "hard-crash-cache.json"
        exported = export_ai_cache(
            self.source_database,
            enabled_config,
            crash_bundle,
        )
        self.assertEqual(1, exported["ambiguous_holds"])

        target = self.database(
            "hard-crash-target.sqlite3",
            self.candidates + [crash_candidate],
            reverse=True,
            shift_ids=True,
        )
        imported = import_ai_cache(target, enabled_config, crash_bundle)
        self.assertEqual(1, imported["ambiguous_holds"])
        target_article = target.article_by_url(
            self.source.slug,
            "https://example.com/blog/hard-crash",
        )

        class MustNotRun:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise AssertionError("fresh runner must not replay the request")

        blocked = MustNotRun()
        target_service = AIService(enabled_config, target, provider=blocked)
        with self.assertRaises(AIGenerationHeld):
            target_service.generate_article(
                int(target_article["id"]),
                task_type="summary",
            )
        self.assertEqual([], blocked.requests)

    def test_fallback_pending_continues_on_deepseek_across_fresh_runner(self):
        openrouter_config = replace(
            self.new_config,
            ai=replace(
                self.new_config.ai,
                enabled=True,
                provider="openrouter",
                fallback_provider="deepseek",
                summary_model=OPENROUTER_MODEL,
                translation_model=OPENROUTER_MODEL,
                api_key_environment="OPENROUTER_API_KEY",
            ),
        )
        fallback_candidate = self.candidate(
            "fallback-pending", "2026-08-01T09:30:00Z"
        )
        self.source_database.commit_candidates(
            self.source,
            [fallback_candidate],
            started_at="2026-08-01T17:02:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="fallback-pending-article",
        )
        source_article = self.source_database.article_by_url(
            self.source.slug,
            "https://example.com/blog/fallback-pending",
        )

        class RejectedOpenRouter:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise ProviderHTTPError(
                    "OpenRouter returned HTTP 429",
                    status=429,
                    retryable=True,
                )

        rejected = RejectedOpenRouter()
        primary = AIService(
            openrouter_config,
            self.source_database,
            provider=rejected,
            automatic_fallback_provider="deepseek",
        )
        with mock.patch.dict(
            "os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=True
        ):
            with self.assertRaises(AIFallbackEligibleError):
                primary.generate_article(
                    int(source_article["id"]), task_type="summary"
                )
        self.assertEqual(1, len(rejected.requests))
        self.assertEqual(
            "fallback_pending",
            self.source_database.list_ai_generation_holds()[0]["hold_class"],
        )

        pending_bundle = self.root / "fallback-pending-cache.json"
        exported = export_ai_cache(
            self.source_database,
            openrouter_config,
            pending_bundle,
        )
        self.assertEqual(1, exported["fallback_pending_holds"])
        target = self.database(
            "fallback-pending-target.sqlite3",
            self.candidates + [fallback_candidate],
            reverse=True,
            shift_ids=True,
        )
        imported = import_ai_cache(target, openrouter_config, pending_bundle)
        self.assertEqual(1, imported["fallback_pending_holds"])
        target_article = target.article_by_url(
            self.source.slug,
            "https://example.com/blog/fallback-pending",
        )

        class MustNotRun:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise AssertionError("pending fallback must not replay OpenRouter")

        must_not_run = MustNotRun()
        fresh_primary = AIService(
            openrouter_config,
            target,
            provider=must_not_run,
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIFallbackEligibleError) as pending:
            fresh_primary.generate_article(
                int(target_article["id"]), task_type="summary"
            )
        self.assertEqual("fallback_pending", pending.exception.reason_code)
        self.assertFalse(pending.exception.provider_call_made)
        self.assertEqual([], must_not_run.requests)

        deepseek_config = replace(
            openrouter_config,
            ai=replace(
                openrouter_config.ai,
                provider="deepseek",
                fallback_provider="",
                summary_model=DEEPSEEK_MODEL,
                translation_model=DEEPSEEK_MODEL,
                api_key_environment="DEEPSEEK_API_KEY",
            ),
        )

        class SuccessfulDeepSeek:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                return ProviderResponse(
                    output_text=json.dumps(
                        {
                            "summary": "Grounded summary.",
                            "key_points": ["Grounded point."],
                            "language": "zh-CN",
                            "basis": "metadata",
                            "limitations": "Metadata only.",
                        }
                    ),
                    usage=ProviderUsage(
                        input_tokens=20,
                        output_tokens=10,
                        total_tokens=30,
                    ),
                    model=DEEPSEEK_MODEL,
                    request_id="deepseek-fallback",
                )

        deepseek = SuccessfulDeepSeek()
        continuation = AIService(
            deepseek_config,
            target,
            provider=deepseek,
            allow_fallback_pending_from="openrouter",
        )
        with mock.patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            result = continuation.generate_article(
                int(target_article["id"]), task_type="summary"
            )
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(1, len(deepseek.requests))
        self.assertEqual([], target.list_ai_generation_holds())

        crash_target = self.database(
            "fallback-pending-crash-target.sqlite3",
            self.candidates + [fallback_candidate],
            reverse=True,
            shift_ids=True,
        )
        import_ai_cache(crash_target, openrouter_config, pending_bundle)
        crash_article = crash_target.article_by_url(
            self.source.slug,
            "https://example.com/blog/fallback-pending",
        )

        class FatalDeepSeek:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                raise KeyboardInterrupt("simulated fallback runner termination")

        fatal_deepseek = FatalDeepSeek()
        crashing_continuation = AIService(
            deepseek_config,
            crash_target,
            provider=fatal_deepseek,
            allow_fallback_pending_from="openrouter",
        )
        with mock.patch.dict(
            "os.environ", {"DEEPSEEK_API_KEY": "test-key"}, clear=True
        ):
            with self.assertRaises(KeyboardInterrupt):
                crashing_continuation.generate_article(
                    int(crash_article["id"]), task_type="summary"
                )
        self.assertEqual(1, len(fatal_deepseek.requests))
        self.assertEqual(
            {"fallback_pending", "ambiguous"},
            {
                str(hold["hold_class"])
                for hold in crash_target.list_ai_generation_holds()
            },
        )

        crash_bundle = self.root / "fallback-inflight-cache.json"
        export_ai_cache(crash_target, deepseek_config, crash_bundle)
        final_target = self.database(
            "fallback-inflight-final-target.sqlite3",
            self.candidates + [fallback_candidate],
            reverse=True,
            shift_ids=True,
        )
        import_ai_cache(final_target, openrouter_config, crash_bundle)
        final_article = final_target.article_by_url(
            self.source.slug,
            "https://example.com/blog/fallback-pending",
        )
        final_openrouter = MustNotRun()
        final_primary = AIService(
            openrouter_config,
            final_target,
            provider=final_openrouter,
            automatic_fallback_provider="deepseek",
        )
        with self.assertRaises(AIGenerationHeld):
            final_primary.generate_article(
                int(final_article["id"]), task_type="summary"
            )
        final_deepseek = MustNotRun()
        final_continuation = AIService(
            deepseek_config,
            final_target,
            provider=final_deepseek,
            allow_fallback_pending_from="openrouter",
        )
        with self.assertRaises(AIGenerationHeld):
            final_continuation.generate_article(
                int(final_article["id"]), task_type="summary"
            )
        self.assertEqual([], final_openrouter.requests)
        self.assertEqual([], final_deepseek.requests)

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
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM ai_artifacts"
                ).fetchone()[0],
            )

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

    def test_prompt_and_schema_contracts_are_strict(self) -> None:
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
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM ai_artifacts"
                ).fetchone()[0],
            )
        self.assertEqual(original_ledger, target.list_ai_usage_ledger())
        self.assertEqual(original_holds, target.list_ai_generation_holds())

    def test_article_change_omits_its_artifacts_and_generation_hold(self) -> None:
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
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            AI_CACHE_PROTOCOL,
            raw_payload.get("protocol"),
            "tracked cloud/ai-cache.json still requires a v3 migration",
        )
        payload = _read_payload(path)
        config = load_config(str(REPOSITORY_ROOT / "config" / "sources.json"))
        _validate_payload(payload, config.sources, verify_hash=True)
        self.assertEqual(
            {
                "protocol",
                "exported_at",
                "bundle_hash",
                "artifacts",
                "usage_ledger",
                "generation_holds",
            },
            set(payload),
        )
        self.assertNotIn("reports", payload)
        # Production state evolves every cycle.  Test structural coverage and
        # uniqueness, never one historical snapshot's exact count or hash.
        self.assertTrue(payload["artifacts"])
        tasks = {entry["task_type"] for entry in payload["artifacts"]}
        self.assertIn("translation", tasks)
        self.assertLessEqual(tasks, {"summary", "translation"})
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
        self.assertLessEqual(
            {
                hold["workload_kind"]
                for hold in payload["generation_holds"]
            },
            {"article", "article_pair"},
        )

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


class UntranslatedCacheImportTests(unittest.TestCase):
    """Integration tests for dropping untranslated artifacts during import.

    Uses the REAL problematic cache records from Production #38 that motivated PR #15:
    - NVIDIA article with English title but Chinese summary
    - Model ML article with both English title and summary
    Plus a good Chinese control that should import successfully.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.openai_source = SourceConfig(
            slug="openai-news",
            name="OpenAI News",
            home_url="https://openai.com/news/",
            fetch_url="https://openai.com/news/rss.xml",
            adapter="rss",
        )
        self.anthropic_source = SourceConfig(
            slug="anthropic-news",
            name="Anthropic News",
            home_url="https://www.anthropic.com/news",
            fetch_url="https://www.anthropic.com/news",
            adapter="anthropic_news",
        )
        self.config = AppConfig(
            sources=[self.openai_source, self.anthropic_source],
            ai=AIConfig(
                enabled=True,
                provider="deepseek",
                fallback_provider="",
                summary_model="deepseek-v4-flash",
                translation_model="deepseek-v4-flash",
                reasoning_effort="none",
                api_key_environment="DEEPSEEK_API_KEY",
            ),
        )
        self.database = self._create_database_with_articles()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_database_with_articles(self) -> Database:
        database = Database(self.root / "test.sqlite3")
        database.initialize()
        database.sync_source_configs([self.openai_source, self.anthropic_source])
        nvidia_candidate = ArticleCandidate(
            source_slug="openai-news",
            external_id="https://openai.com/index/nvidia/chatgpt-work",
            url="https://openai.com/index/nvidia/chatgpt-work",
            title="How NVIDIA scales expertise with ChatGPT Work",
            summary="NVIDIA teams use ChatGPT Work to reduce manual tasks and connect rapidly changing signals.",
            author="OpenAI",
            category="News",
            published_at="2026-08-19T00:00:00Z",
            content_hash="e89ce570246502ed8924526a83ec55b785eb14c4b869547d5e35d2c17ffb52fa",
        )
        model_ml_candidate = ArticleCandidate(
            source_slug="openai-news",
            external_id="https://openai.com/index/model-ml",
            url="https://openai.com/index/model-ml",
            title="Model ML completes finance work more efficiently with GPT-5.6 Sol",
            summary="Model ML uses GPT-5.6 Sol to carry finance work from research and analysis through editable, traceable PowerPoint decks and Excel workbooks.",
            author="OpenAI",
            category="News",
            published_at="2026-08-11T00:00:00Z",
            content_hash="3f1fde4002e985b92d72cc7fb6d50652fc58267c611712d44730c028915a1692",
        )
        anthropic_candidate = ArticleCandidate(
            source_slug="anthropic-news",
            external_id="https://www.anthropic.com/news/anthropic-economic-index-connector",
            url="https://www.anthropic.com/news/anthropic-economic-index-connector",
            title="Ask Claude about the Anthropic Economic Index",
            summary="",
            author="Anthropic",
            category="News",
            published_at="2026-08-02T00:00:00Z",
            content_hash="4d5dd7244ed3325a0018cd327431dbc930e33283ed81d9bd4b1982b5ae1b9790",
        )
        database.commit_candidates(
            self.openai_source,
            [nvidia_candidate, model_ml_candidate],
            started_at="2026-08-19T10:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="openai-test",
        )
        database.commit_candidates(
            self.anthropic_source,
            [anthropic_candidate],
            started_at="2026-08-19T10:00:00Z",
            http_status=200,
            etag="",
            last_modified="",
            body_hash="anthropic-test",
        )
        return database

    def _create_test_bundle(self) -> Path:
        """Create a minimal cache bundle with the real problematic records."""
        nvidia_artifact = {
            "cache_key": "f786f17e31c850491728ccf97a943491e9ba0ab2bdbde1bb2f72bbaffc5acdc5",
            "article": {
                "source_slug": "openai-news",
                "external_id": "https://openai.com/index/nvidia/chatgpt-work",
                "canonical_url": "https://openai.com/index/nvidia/chatgpt-work",
                "content_hash": "e89ce570246502ed8924526a83ec55b785eb14c4b869547d5e35d2c17ffb52fa",
            },
            "task_type": "translation",
            "input_scope": "metadata",
            "source_language": "unknown",
            "target_language": "zh-CN",
            "prompt_version": "ai-enrichment-v1",
            "prompt_hash": "02fd7530627fb63bdf764af71f06077c7b0298122a834afbb846a69b812e3d8d",
            "response_schema_version": "ai-output-v1",
            "response_schema_hash": "852b5b36ced43460d3c11d1db23dfc71175f8316700c3109a584de3626d30a4e",
            "provider": "openrouter",
            "requested_model": "openrouter/free",
            "resolved_model": "dots-studio/dots-3-note-preview:free",
            "generation_params_hash": "2d3ca133780779191f20a20163772118e1850f4bde63ea433cc340d1dff2cf80",
            "output": {
                "language": "zh-CN",
                "limitations": "仅基于提供的摘要信息，未包含完整文章内容；无法确认具体实施细节、团队规模或量化成果。",
                "title": "How NVIDIA scales expertise with ChatGPT Work",
                "publisher_summary": "NVIDIA团队使用ChatGPT Work来减少手动任务、连接快速变化的信号，并在全球范围内扩展成功的流程。",
            },
            "output_hash": "00a6026ad0c3c00ea2e4c652e3abf4f55547d0826666eba9b60a86065853cf5f",
            "input_truncated": False,
            "created_at": "2026-08-19T04:55:27Z",
        }
        model_ml_artifact = {
            "cache_key": "84fd7321bb0a595fb42e78ceb0d749d163389ac04541a7b39f3a20a10d12ad04",
            "article": {
                "source_slug": "openai-news",
                "external_id": "https://openai.com/index/model-ml",
                "canonical_url": "https://openai.com/index/model-ml",
                "content_hash": "3f1fde4002e985b92d72cc7fb6d50652fc58267c611712d44730c028915a1692",
            },
            "task_type": "translation",
            "input_scope": "metadata",
            "source_language": "unknown",
            "target_language": "zh-CN",
            "prompt_version": "ai-enrichment-v1",
            "prompt_hash": "02fd7530627fb63bdf764af71f06077c7b0298122a834afbb846a69b812e3d8d",
            "response_schema_version": "ai-output-v1",
            "response_schema_hash": "852b5b36ced43460d3c11d1db23dfc71175f8316700c3109a584de3626d30a4e",
            "provider": "openrouter",
            "requested_model": "openrouter/free",
            "resolved_model": "google/gemma-4-26b-a4b-it:free",
            "generation_params_hash": "2d3ca133780779191f20a20163772118e1850f4bde63ea433cc340d1dff2cf80",
            "output": {
                "language": "zh-CN",
                "limitations": "Based on the provided summary only.",
                "title": "Model ML completes finance work more efficiently with GPT-5.6 Sol",
                "publisher_summary": "Model ML uses GPT-5.6 Sol to carry finance work from research and analysis through editable, traceable PowerPoint decks and Excel workbooks.",
            },
            "output_hash": "04779266f7e88358cf49a44776e3d2d2fb55d9f100bb73a48b3ac7bace505594",
            "input_truncated": False,
            "created_at": "2026-08-11T05:32:48Z",
        }
        good_chinese_artifact = {
            "cache_key": "50f9dc54caf64efb692074d76d221c41ea501254e746bd1fc220904c3653bf6b",
            "article": {
                "source_slug": "anthropic-news",
                "external_id": "https://www.anthropic.com/news/anthropic-economic-index-connector",
                "canonical_url": "https://www.anthropic.com/news/anthropic-economic-index-connector",
                "content_hash": "4d5dd7244ed3325a0018cd327431dbc930e33283ed81d9bd4b1982b5ae1b9790",
            },
            "task_type": "translation",
            "input_scope": "metadata",
            "source_language": "unknown",
            "target_language": "zh-CN",
            "prompt_version": "ai-enrichment-v1",
            "prompt_hash": "02fd7530627fb63bdf764af71f06077c7b0298122a834afbb846a69b812e3d8d",
            "response_schema_version": "ai-output-v1",
            "response_schema_hash": "852b5b36ced43460d3c11d1db23dfc71175f8316700c3109a584de3626d30a4e",
            "provider": "chatgpt-codex-subscription",
            "requested_model": "gpt-5.6-luna",
            "resolved_model": "gpt-5.6-luna",
            "generation_params_hash": "bd6f4e320e48ae894fbf2375ac97f856b960ac8af5d24448db550c33fe07a790",
            "output": {
                "language": "zh-CN",
                "limitations": "发布方摘要为空，因此仅翻译标题；未提供正文。",
                "title": "向 Claude 询问 Anthropic Economic Index",
                "publisher_summary": "",
            },
            "output_hash": "907d2ad802fa64028f35742f3e792767a625751a9cec8914d1d7273ba63775b1",
            "input_truncated": False,
            "created_at": "2026-08-02T07:12:37Z",
        }
        artifacts = sorted(
            [model_ml_artifact, nvidia_artifact, good_chinese_artifact],
            key=lambda a: (
                a["article"]["source_slug"],
                a["article"]["external_id"],
                a["article"]["canonical_url"],
                a["task_type"],
                a["target_language"],
            ),
        )
        bundle = {
            "protocol": AI_CACHE_PROTOCOL,
            "exported_at": "2026-08-19T12:00:00Z",
            "bundle_hash": "0" * 64,
            "artifacts": artifacts,
            "usage_ledger": [],
            "generation_holds": [],
        }
        bundle["bundle_hash"] = _cache_hash(bundle)
        bundle_path = self.root / "test-bundle.json"
        with bundle_path.open("w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        return bundle_path

    def test_import_drops_untranslated_artifacts(self):
        """Untranslated translation artifacts should be dropped during import."""
        bundle_path = self._create_test_bundle()
        result = import_ai_cache(self.database, self.config, bundle_path)
        self.assertEqual(2, result["dropped_untranslated"])
        self.assertEqual(1, result["artifacts"])
        self.assertEqual(1, result["inserted_artifacts"])

    def test_current_article_artifact_returns_none_for_dropped(self):
        """current_article_artifact should return None for dropped untranslated artifacts."""
        bundle_path = self._create_test_bundle()
        import_ai_cache(self.database, self.config, bundle_path)
        service = AIService(self.config, self.database)
        articles = self.database.list_articles(limit=10)
        nvidia_article = next(
            a for a in articles
            if "nvidia" in a["canonical_url"]
        )
        model_ml_article = next(
            a for a in articles
            if "model-ml" in a["canonical_url"]
        )
        anthropic_article = next(
            a for a in articles
            if "anthropic" in a["canonical_url"]
        )
        nvidia_result = service.current_article_artifact(
            int(nvidia_article["id"]),
            task_type="translation",
            target_language="zh-CN",
        )
        self.assertIsNone(nvidia_result)
        model_ml_result = service.current_article_artifact(
            int(model_ml_article["id"]),
            task_type="translation",
            target_language="zh-CN",
        )
        self.assertIsNone(model_ml_result)
        anthropic_result = service.current_article_artifact(
            int(anthropic_article["id"]),
            task_type="translation",
            target_language="zh-CN",
        )
        self.assertIsNotNone(anthropic_result)
        self.assertIn("向 Claude 询问", anthropic_result["output"]["title"])

    def test_mixed_chinese_product_names_imports_as_hit(self):
        """Translations with mixed Chinese + product names (NVIDIA/ChatGPT/GPT) should import."""
        mixed_artifact = {
            "cache_key": "",
            "article": {
                "source_slug": "openai-news",
                "external_id": "https://openai.com/index/nvidia/chatgpt-work",
                "canonical_url": "https://openai.com/index/nvidia/chatgpt-work",
                "content_hash": "e89ce570246502ed8924526a83ec55b785eb14c4b869547d5e35d2c17ffb52fa",
            },
            "task_type": "translation",
            "input_scope": "metadata",
            "source_language": "unknown",
            "target_language": "zh-CN",
            "prompt_version": "ai-enrichment-v1",
            "prompt_hash": "02fd7530627fb63bdf764af71f06077c7b0298122a834afbb846a69b812e3d8d",
            "response_schema_version": "ai-output-v1",
            "response_schema_hash": "852b5b36ced43460d3c11d1db23dfc71175f8316700c3109a584de3626d30a4e",
            "provider": "deepseek",
            "requested_model": "deepseek-v4-flash",
            "resolved_model": "deepseek-v4-flash",
            "generation_params_hash": "2d3ca133780779191f20a20163772118e1850f4bde63ea433cc340d1dff2cf80",
            "output": {
                "language": "zh-CN",
                "limitations": "",
                "title": "NVIDIA 如何通过 ChatGPT Work 扩展专业知识",
                "publisher_summary": "NVIDIA 团队使用 ChatGPT Work 和 GPT-5.6 减少手动任务并扩展流程。",
            },
            "output_hash": stable_hash(canonical_json({
                "language": "zh-CN",
                "limitations": "",
                "title": "NVIDIA 如何通过 ChatGPT Work 扩展专业知识",
                "publisher_summary": "NVIDIA 团队使用 ChatGPT Work 和 GPT-5.6 减少手动任务并扩展流程。",
            })),
            "input_truncated": False,
            "created_at": "2026-08-19T10:00:00Z",
        }
        mixed_artifact["cache_key"] = _entry_hash(mixed_artifact)
        bundle = {
            "protocol": AI_CACHE_PROTOCOL,
            "exported_at": "2026-08-19T12:00:00Z",
            "bundle_hash": "0" * 64,
            "artifacts": [mixed_artifact],
            "usage_ledger": [],
            "generation_holds": [],
        }
        bundle["bundle_hash"] = _cache_hash(bundle)
        bundle_path = self.root / "mixed-bundle.json"
        with bundle_path.open("w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        result = import_ai_cache(self.database, self.config, bundle_path)
        self.assertEqual(0, result["dropped_untranslated"])
        self.assertEqual(1, result["artifacts"])
        self.assertEqual(1, result["inserted_artifacts"])
        service = AIService(self.config, self.database)
        articles = self.database.list_articles(limit=10)
        nvidia_article = next(
            a for a in articles
            if "nvidia" in a["canonical_url"]
        )
        nvidia_result = service.current_article_artifact(
            int(nvidia_article["id"]),
            task_type="translation",
            target_language="zh-CN",
        )
        self.assertIsNotNone(nvidia_result)
        self.assertIn("NVIDIA 如何通过", nvidia_result["output"]["title"])

    def test_regeneration_after_dropped_import_produces_cjk_titles(self):
        """After dropping untranslated artifacts, regeneration should produce CJK titles."""
        bundle_path = self._create_test_bundle()
        import_ai_cache(self.database, self.config, bundle_path)

        class ChineseTranslationProvider:
            def __init__(self):
                self.generate_calls = []

            def generate(self, request):
                self.generate_calls.append(request)
                input_value = json.loads(request.input_text)
                title = input_value.get("title", "")
                summary = input_value.get("publisher_summary", "")
                if "NVIDIA" in title:
                    output = {
                        "title": "NVIDIA 如何通过 ChatGPT Work 扩展专业知识",
                        "publisher_summary": "NVIDIA 团队使用 ChatGPT Work 减少手动任务并扩展流程。",
                        "language": "zh-CN",
                        "limitations": "",
                    }
                elif "Model ML" in title:
                    output = {
                        "title": "Model ML 如何使用 GPT-5.6 Sol 更高效地完成财务工作",
                        "publisher_summary": "Model ML 使用 GPT-5.6 Sol 完成财务研究和分析工作。",
                        "language": "zh-CN",
                        "limitations": "",
                    }
                else:
                    output = {
                        "title": "翻译：%s" % title,
                        "publisher_summary": "翻译：%s" % summary if summary else "",
                        "language": "zh-CN",
                        "limitations": "",
                    }
                return ProviderResponse(
                    output_text=json.dumps(output, ensure_ascii=False),
                    usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150),
                    model="deepseek-v4-flash",
                    request_id="test-request",
                )

        provider = ChineseTranslationProvider()
        service = AIService(self.config, self.database, provider=provider)
        articles = self.database.list_articles(limit=10)
        nvidia_article = next(
            a for a in articles
            if "nvidia" in a["canonical_url"]
        )
        model_ml_article = next(
            a for a in articles
            if "model-ml" in a["canonical_url"]
        )
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}):
            nvidia_result = service.generate_article(
                int(nvidia_article["id"]),
                task_type="translation",
                target_language="zh-CN",
            )
            model_ml_result = service.generate_article(
                int(model_ml_article["id"]),
                task_type="translation",
                target_language="zh-CN",
            )
        self.assertEqual(2, len(provider.generate_calls))
        self.assertIn("NVIDIA 如何通过", nvidia_result["output"]["title"])
        self.assertIn("Model ML 如何使用", model_ml_result["output"]["title"])
        export_path = self.root / "re-export.json"
        export_result = export_ai_cache(self.database, self.config, export_path)
        with export_path.open("r", encoding="utf-8") as f:
            exported = json.load(f)
        exported_titles = [
            a["output"]["title"]
            for a in exported["artifacts"]
            if a["task_type"] == "translation"
        ]
        self.assertNotIn(
            "How NVIDIA scales expertise with ChatGPT Work",
            exported_titles,
        )
        self.assertNotIn(
            "Model ML completes finance work more efficiently with GPT-5.6 Sol",
            exported_titles,
        )
        chinese_titles = [t for t in exported_titles if any(0x4E00 <= ord(c) <= 0x9FFF for c in t)]
        self.assertEqual(len(exported_titles), len(chinese_titles))


if __name__ == "__main__":
    unittest.main()
