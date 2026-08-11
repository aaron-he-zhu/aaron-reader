import hashlib
import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.database import (  # noqa: E402
    AIBudgetExceeded,
    AIJobConflict,
    Database,
    SCHEMA_VERSION,
)


class AIDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "reader.sqlite3"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def job(self, key):
        return self.database.ensure_ai_job(
            artifact_key=key,
            article_id=None,
            task_type="summary",
            input_scope="metadata",
            target_language="zh-CN",
            request={"version": 1, "key": key},
        )

    def reserve(
        self,
        job_id,
        key,
        total_cap=1000,
        *,
        requested_model="test-model",
        requested_provider="unknown",
    ):
        return self.database.reserve_ai_attempt(
            job_id=job_id,
            idempotency_key=key,
            requested_model=requested_model,
            requested_provider=requested_provider,
            estimated_input_tokens=6,
            reserved_output_tokens=4,
            reserved_cost_micros=0,
            price_snapshot={},
            daily_started_at="2020-01-01T00:00:00Z",
            monthly_started_at="2020-01-01T00:00:00Z",
            daily_reset_at="2999-01-02T00:00:00Z",
            monthly_reset_at="2999-02-01T00:00:00Z",
            daily_max_requests=100,
            daily_max_total_tokens=total_cap,
            daily_max_cost_micros=0,
            monthly_max_requests=100,
            monthly_max_total_tokens=total_cap,
            monthly_max_cost_micros=0,
        )

    @staticmethod
    def generation_hold(hold_class, *, include_timestamps=True):
        descriptor = {
            "protocol": "aaron-reader-test-generation-hold/v1",
            "workload_kind": "article",
            "fixture": "provider-fallback",
        }
        encoded = json.dumps(
            descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        hold = {
            "hold_key": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "workload_kind": "article",
            "hold_class": hold_class,
            "descriptor": descriptor,
        }
        if include_timestamps:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            hold.update({"first_seen_at": now, "last_seen_at": now})
        return hold

    @staticmethod
    def usage_entry(**overrides):
        entry = {
            "timezone": "America/Los_Angeles",
            "day_start": "2020-01-01T08:00:00Z",
            "day_end": "2020-01-02T08:00:00Z",
            "covered_through": "2020-01-02T08:00:00Z",
            "requests": 1,
            "confirmed_requests": 1,
            "unconfirmed_requests": 0,
            "input_tokens": 7,
            "cached_input_tokens": 0,
            "cache_miss_input_tokens": 7,
            "cache_write_input_tokens": 0,
            "output_tokens": 3,
            "reasoning_tokens": 0,
            "total_tokens": 10,
            "reserved_total_tokens_for_unconfirmed": 0,
            "cost_micros": 0,
            "reserved_cost_micros_for_unconfirmed": 0,
        }
        entry.update(overrides)
        return entry

    def test_v2_marker_upgrades_without_losing_existing_data(self):
        with self.database.connect() as connection:
            connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy_marker(value) VALUES('kept')")
            connection.execute(
                "UPDATE app_meta SET value='2' WHERE key='schema_version'"
            )
            for table in (
                "ai_attempts",
                "ai_jobs",
                "ai_artifacts",
                "article_content_snapshots",
            ):
                connection.execute("DROP TABLE %s" % table)
        self.database.initialize()
        with self.database.connect() as connection:
            self.assertEqual(
                str(SCHEMA_VERSION),
                connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "kept", connection.execute("SELECT value FROM legacy_marker").fetchone()[0]
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='ai_artifacts'"
                ).fetchone()
            )

    def test_future_schema_is_rejected_without_being_overwritten(self):
        future = SCHEMA_VERSION + 10
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE app_meta SET value=? WHERE key='schema_version'", (str(future),)
            )
        with self.assertRaisesRegex(ValueError, "newer than supported"):
            self.database.initialize()
        with self.database.connect() as connection:
            self.assertEqual(
                str(future),
                connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )

    def test_unmarked_v3_attempt_table_migrates_without_losing_audit_data(self):
        job = self.job("legacy-attempt-artifact")
        attempt = self.reserve(int(job["id"]), "legacy-provider-key")
        with self.database.connect() as connection:
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_attempts'"
            ).fetchone()[0]
            legacy_sql = table_sql.replace(
                "idempotency_key TEXT NOT NULL,",
                "idempotency_key TEXT NOT NULL UNIQUE,",
            )
            self.assertNotEqual(table_sql, legacy_sql)
            connection.execute("ALTER TABLE ai_attempts RENAME TO ai_attempts_old")
            connection.execute(legacy_sql)
            connection.execute(
                "INSERT INTO ai_attempts SELECT * FROM ai_attempts_old"
            )
            connection.execute("DROP TABLE ai_attempts_old")
            connection.execute(
                "DELETE FROM app_meta WHERE key='schema_version'"
            )

        self.database.initialize()

        migrated = self.database.list_ai_attempts()
        self.assertEqual(1, len(migrated))
        self.assertEqual(attempt["id"], migrated[0]["id"])
        self.assertEqual("legacy-provider-key", migrated[0]["idempotency_key"])
        with self.database.connect() as connection:
            version = connection.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()[0]
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_attempts'"
            ).fetchone()[0]
        self.assertEqual(str(SCHEMA_VERSION), version)
        self.assertNotIn("idempotency_key TEXT NOT NULL UNIQUE", table_sql)

    def test_requested_provider_is_persisted_with_each_attempt(self):
        attempt = self.reserve(
            int(self.job("provider-audit")["id"]),
            "provider-audit-key",
            requested_model="openrouter/free",
            requested_provider="openrouter",
        )
        self.assertEqual("openrouter", attempt["requested_provider"])
        self.assertEqual(
            "openrouter",
            self.database.list_ai_attempts()[0]["requested_provider"],
        )

    def test_v6_migration_backfills_requested_provider_and_preserves_holds(self):
        expected_providers = {
            "openrouter/free": "openrouter",
            "deepseek-v4-flash": "deepseek",
            "private-model": "unknown",
        }
        for index, model in enumerate(expected_providers):
            self.reserve(
                int(self.job("legacy-provider-%d" % index)["id"]),
                "legacy-provider-key-%d" % index,
                requested_model=model,
                requested_provider="discarded-by-v6-fixture",
            )
        legacy_hold = self.generation_hold("paid_failure")
        self.database.replace_ai_generation_holds([legacy_hold])

        with self.database.connect() as connection:
            attempt_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='ai_attempts'"
            ).fetchone()[0]
            legacy_attempt_sql = attempt_table_sql.replace(
                "requested_provider TEXT NOT NULL DEFAULT 'unknown',", ""
            )
            self.assertNotEqual(attempt_table_sql, legacy_attempt_sql)
            attempt_columns = [
                str(row[1])
                for row in connection.execute("PRAGMA table_info(ai_attempts)")
                if str(row[1]) != "requested_provider"
            ]
            columns_sql = ", ".join(attempt_columns)
            connection.execute("DROP INDEX idx_ai_attempts_started")
            connection.execute("ALTER TABLE ai_attempts RENAME TO ai_attempts_v7")
            connection.execute(legacy_attempt_sql)
            connection.execute(
                "INSERT INTO ai_attempts(%s) SELECT %s FROM ai_attempts_v7"
                % (columns_sql, columns_sql)
            )
            connection.execute("DROP TABLE ai_attempts_v7")

            hold_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='ai_generation_holds'"
            ).fetchone()[0]
            legacy_hold_sql = hold_table_sql.replace(
                ", 'fallback_pending'", ""
            )
            legacy_hold_sql = legacy_hold_sql.replace(
                "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),",
                "",
            )
            self.assertNotEqual(hold_table_sql, legacy_hold_sql)
            connection.execute("DROP INDEX idx_ai_generation_holds_seen")
            connection.execute(
                "ALTER TABLE ai_generation_holds RENAME TO ai_generation_holds_v7"
            )
            connection.execute(legacy_hold_sql)
            connection.execute(
                """
                INSERT INTO ai_generation_holds(
                    hold_key, workload_kind, hold_class, descriptor_json,
                    first_seen_at, last_seen_at
                )
                SELECT hold_key, workload_kind, hold_class, descriptor_json,
                    first_seen_at, last_seen_at
                FROM ai_generation_holds_v7
                """
            )
            connection.execute("DROP TABLE ai_generation_holds_v7")
            connection.execute(
                "UPDATE app_meta SET value='6' WHERE key='schema_version'"
            )
            connection.execute(
                "DELETE FROM app_meta "
                "WHERE key='ai_generation_hold_revision'"
            )

        self.database.initialize()

        attempts = self.database.list_ai_attempts()
        self.assertEqual(
            expected_providers,
            {
                str(attempt["requested_model"]): str(attempt["requested_provider"])
                for attempt in attempts
            },
        )
        self.assertEqual([legacy_hold], self.database.list_ai_generation_holds())
        with self.database.connect() as connection:
            version = connection.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()[0]
            hold_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='ai_generation_holds'"
            ).fetchone()[0]
            hold_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(ai_generation_holds)"
                ).fetchall()
            }
        self.assertEqual(str(SCHEMA_VERSION), version)
        self.assertIn("fallback_pending", hold_table_sql)
        self.assertIn("revision", hold_columns)
        migrated_hold = self.database.ai_generation_hold(
            legacy_hold["hold_key"],
            include_revision=True,
        )
        self.assertEqual(1, migrated_hold["revision"])

    def test_v9_migration_retires_ai_briefs_and_preserves_article_audit_state(self):
        timestamp = "2026-08-01T17:00:00Z"
        usage = self.usage_entry(
            day_start="2026-08-01T07:00:00Z",
            day_end="2026-08-02T07:00:00Z",
            covered_through="2026-08-01T17:00:00Z",
            requests=2,
            confirmed_requests=1,
            unconfirmed_requests=1,
            input_tokens=90,
            cached_input_tokens=10,
            cache_miss_input_tokens=80,
            output_tokens=30,
            total_tokens=120,
            reserved_total_tokens_for_unconfirmed=200,
        )
        self.database.replace_ai_usage_ledger([usage])

        retained_hold_keys = set()
        removed_hold_keys = set()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    slug, name, home_url, fetch_url, adapter, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy",
                    "Legacy",
                    "https://example.com/",
                    "https://example.com/feed.xml",
                    "rss",
                    timestamp,
                ),
            )
            article_cursor = connection.execute(
                """
                INSERT INTO articles(
                    source_slug, external_id, canonical_url, title, summary,
                    discovered_at, updated_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy",
                    "article",
                    "https://example.com/article",
                    "Legacy article",
                    "Publisher summary",
                    timestamp,
                    timestamp,
                    "article-content-v1",
                ),
            )
            article_id = int(article_cursor.lastrowid)

            def insert_artifact(
                *,
                article,
                task_type,
                input_scope,
                artifact_key,
                output,
            ):
                output_json = json.dumps(
                    output,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO ai_artifacts(
                        article_id, task_type, input_scope, target_language,
                        artifact_key, input_hash, article_content_hash,
                        prompt_version, prompt_hash, response_schema_version,
                        response_schema_hash, provider, requested_model,
                        resolved_model, generation_params_hash, output_json,
                        output_text, output_hash, status, input_truncated,
                        created_at
                    ) VALUES (
                        ?, ?, ?, 'zh-CN', ?, ?, ?, 'legacy-v1', ?,
                        'legacy-v1', ?, 'deepseek', 'deepseek-v4-flash',
                        'deepseek-v4-flash', ?, ?, ?, ?, 'succeeded', 0, ?
                    )
                    """,
                    (
                        article,
                        task_type,
                        input_scope,
                        artifact_key,
                        hashlib.sha256(
                            ("%s-input" % artifact_key).encode("utf-8")
                        ).hexdigest(),
                        "article-content-v1" if article is not None else "",
                        hashlib.sha256(b"legacy-prompt").hexdigest(),
                        hashlib.sha256(b"legacy-schema").hexdigest(),
                        hashlib.sha256(b"legacy-generation").hexdigest(),
                        output_json,
                        str(output),
                        hashlib.sha256(output_json.encode("utf-8")).hexdigest(),
                        timestamp,
                    ),
                )
                return int(cursor.lastrowid)

            article_artifact_id = insert_artifact(
                article=article_id,
                task_type="summary",
                input_scope="metadata",
                artifact_key="legacy-article-summary",
                output={
                    "summary": "Grounded article summary",
                    "language": "zh-CN",
                },
            )
            digest_artifact_id = insert_artifact(
                article=None,
                task_type="digest",
                input_scope="digest",
                artifact_key="legacy-daily-brief",
                output={
                    "headline": "Legacy daily brief",
                    "items": [{"article_id": article_id}],
                    "language": "zh-CN",
                },
            )

            connection.executescript(
                """
                CREATE TABLE ai_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_key TEXT NOT NULL UNIQUE,
                    period TEXT NOT NULL CHECK(period IN ('daily', 'weekly')),
                    timezone TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    target_language TEXT NOT NULL,
                    article_ids_json TEXT NOT NULL,
                    article_content_hash TEXT NOT NULL,
                    artifact_id INTEGER NOT NULL UNIQUE
                        REFERENCES ai_artifacts(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX idx_ai_reports_latest
                    ON ai_reports(
                        period, target_language, created_at DESC, id DESC
                    );
                """
            )
            connection.execute(
                """
                INSERT INTO ai_reports(
                    report_key, period, timezone, local_date, period_start,
                    period_end, target_language, article_ids_json,
                    article_content_hash, artifact_id, created_at
                ) VALUES (?, 'daily', 'America/Los_Angeles', '2026-08-01',
                    '2026-08-01T07:00:00Z', '2026-08-01T17:00:00Z',
                    'zh-CN', ?, ?, ?, ?)
                """,
                (
                    "legacy-report",
                    json.dumps([article_id]),
                    "legacy-report-article-set",
                    digest_artifact_id,
                    timestamp,
                ),
            )

            article_job_cursor = connection.execute(
                """
                INSERT INTO ai_jobs(
                    artifact_key, article_id, task_type, input_scope,
                    target_language, request_json, trigger_kind, state,
                    artifact_id, created_at, updated_at
                ) VALUES (?, ?, 'summary', 'metadata', 'zh-CN', ?, 'batch',
                    'succeeded', ?, ?, ?)
                """,
                (
                    "legacy-article-summary",
                    article_id,
                    '{"task_type":"summary"}',
                    article_artifact_id,
                    timestamp,
                    timestamp,
                ),
            )
            article_job_id = int(article_job_cursor.lastrowid)
            completed_digest_job_cursor = connection.execute(
                """
                INSERT INTO ai_jobs(
                    artifact_key, task_type, input_scope, target_language,
                    request_json, trigger_kind, state, attempt_count,
                    artifact_id, created_at, updated_at
                ) VALUES (?, 'digest', 'digest', 'zh-CN', ?, 'scheduled',
                    'succeeded', 1, ?, ?, ?)
                """,
                (
                    "legacy-daily-brief",
                    '{"period":"daily","article_ids":[1]}',
                    digest_artifact_id,
                    timestamp,
                    timestamp,
                ),
            )
            completed_digest_job_id = int(completed_digest_job_cursor.lastrowid)
            pending_digest_job_cursor = connection.execute(
                """
                INSERT INTO ai_jobs(
                    artifact_key, task_type, input_scope, target_language,
                    request_json, trigger_kind, state, attempt_count,
                    created_at, updated_at
                ) VALUES (?, 'digest', 'digest', 'en', ?, 'scheduled',
                    'sent', 1, ?, ?)
                """,
                (
                    "legacy-weekly-brief-pending",
                    '{"period":"weekly","article_ids":[1],"secret":"scrub-me"}',
                    timestamp,
                    timestamp,
                ),
            )
            pending_digest_job_id = int(pending_digest_job_cursor.lastrowid)

            completed_attempt_cursor = connection.execute(
                """
                INSERT INTO ai_attempts(
                    job_id, attempt_number, idempotency_key, state,
                    request_started_at, response_received_at,
                    provider_request_id, requested_provider, requested_model,
                    resolved_model, estimated_input_tokens,
                    reserved_output_tokens, reserved_total_tokens,
                    actual_input_tokens, actual_cached_input_tokens,
                    actual_cache_write_tokens, actual_output_tokens,
                    actual_reasoning_tokens, actual_total_tokens,
                    reservation_active, finish_reason, response_hash
                ) VALUES (?, 1, ?, 'succeeded', ?, ?, ?, 'deepseek',
                    'deepseek-v4-flash', 'deepseek-v4-flash', 100, 100, 200,
                    90, 10, 0, 30, 0, 120, 0, 'stop', ?)
                """,
                (
                    completed_digest_job_id,
                    "legacy-completed-digest-attempt",
                    timestamp,
                    timestamp,
                    "legacy-provider-request",
                    hashlib.sha256(b"legacy-response").hexdigest(),
                ),
            )
            completed_attempt_id = int(completed_attempt_cursor.lastrowid)
            pending_attempt_cursor = connection.execute(
                """
                INSERT INTO ai_attempts(
                    job_id, attempt_number, idempotency_key, state,
                    request_started_at, requested_provider, requested_model,
                    estimated_input_tokens, reserved_output_tokens,
                    reserved_total_tokens, reservation_active
                ) VALUES (?, 1, ?, 'sent', ?, 'deepseek',
                    'deepseek-v4-flash', 100, 100, 200, 1)
                """,
                (
                    pending_digest_job_id,
                    "legacy-pending-digest-attempt",
                    timestamp,
                ),
            )
            pending_attempt_id = int(pending_attempt_cursor.lastrowid)

            for workload_kind, hold_class in (
                ("article", "ambiguous"),
                ("article_pair", "fallback_pending"),
                ("digest", "paid_failure"),
                ("report", "ambiguous"),
            ):
                descriptor = {
                    "protocol": "aaron-reader-test-generation-hold/v1",
                    "workload_kind": workload_kind,
                    "fixture": "v8-to-v9",
                }
                encoded = json.dumps(
                    descriptor,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                hold_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                connection.execute(
                    """
                    INSERT INTO ai_generation_holds(
                        hold_key, workload_kind, hold_class, descriptor_json,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hold_key,
                        workload_kind,
                        hold_class,
                        encoded,
                        timestamp,
                        timestamp,
                    ),
                )
                if workload_kind in {"article", "article_pair"}:
                    retained_hold_keys.add(hold_key)
                else:
                    removed_hold_keys.add(hold_key)

            connection.execute(
                "UPDATE app_meta SET value='8' WHERE key='schema_version'"
            )

        self.database.initialize()

        self.assertEqual([usage], self.database.list_ai_usage_ledger())
        attempts = {
            int(attempt["id"]): attempt
            for attempt in self.database.list_ai_attempts()
        }
        self.assertEqual(2, len(attempts))
        self.assertEqual("succeeded", attempts[completed_attempt_id]["state"])
        self.assertEqual(120, attempts[completed_attempt_id]["actual_total_tokens"])
        self.assertEqual("sent", attempts[pending_attempt_id]["state"])
        self.assertEqual(
            "legacy-pending-digest-attempt",
            attempts[pending_attempt_id]["idempotency_key"],
        )
        self.assertEqual(
            retained_hold_keys,
            {
                str(hold["hold_key"])
                for hold in self.database.list_ai_generation_holds()
            },
        )
        self.assertTrue(
            removed_hold_keys.isdisjoint(
                {
                    str(hold["hold_key"])
                    for hold in self.database.list_ai_generation_holds()
                }
            )
        )

        article_job = self.database.ai_job(article_job_id)
        self.assertEqual("succeeded", article_job["state"])
        self.assertEqual('{"task_type":"summary"}', article_job["request_json"])
        self.assertEqual(article_artifact_id, int(article_job["artifact_id"]))

        completed_digest_job = self.database.ai_job(completed_digest_job_id)
        self.assertEqual("succeeded", completed_digest_job["state"])
        self.assertEqual("{}", completed_digest_job["request_json"])
        self.assertIsNone(completed_digest_job["artifact_id"])
        pending_digest_job = self.database.ai_job(pending_digest_job_id)
        self.assertEqual("cancelled", pending_digest_job["state"])
        self.assertEqual("{}", pending_digest_job["request_json"])

        with self.database.connect() as connection:
            self.assertEqual(
                str(SCHEMA_VERSION),
                connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='ai_reports'"
                ).fetchone()
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM ai_artifacts WHERE task_type='digest'"
                ).fetchone()[0],
            )
            retained_artifact = connection.execute(
                "SELECT task_type, input_scope FROM ai_artifacts WHERE id=?",
                (article_artifact_id,),
            ).fetchone()
            self.assertEqual(("summary", "metadata"), tuple(retained_artifact))
            retained_article = connection.execute(
                "SELECT title, summary FROM articles WHERE id=?",
                (article_id,),
            ).fetchone()
            self.assertEqual(
                ("Legacy article", "Publisher summary"),
                tuple(retained_article),
            )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_fallback_pending_hold_validation_and_risk_priority(self):
        pending = self.generation_hold("fallback_pending")
        self.database.replace_ai_generation_holds([pending])
        self.assertEqual(
            "fallback_pending",
            self.database.ai_generation_hold(pending["hold_key"])["hold_class"],
        )

        invalid = {**pending, "hold_class": "retryable"}
        with self.assertRaisesRegex(ValueError, "hold class is invalid"):
            self.database.replace_ai_generation_holds([invalid])
        self.assertEqual([pending], self.database.list_ai_generation_holds())

        attempt = self.reserve(
            int(self.job("hold-risk-priority")["id"]),
            "hold-risk-priority-key",
        )

        def upsert(hold_class):
            self.database.fail_ai_attempt(
                attempt_id=int(attempt["id"]),
                job_state="permanent_failed",
                error_class="provider_known",
                error_code="fixture",
                error_message="fixture",
                generation_hold=self.generation_hold(
                    hold_class, include_timestamps=False
                ),
            )
            return self.database.ai_generation_hold(pending["hold_key"])[
                "hold_class"
            ]

        self.assertEqual("paid_failure", upsert("paid_failure"))
        self.assertEqual("paid_failure", upsert("fallback_pending"))
        self.assertEqual("ambiguous", upsert("ambiguous"))
        self.assertEqual("ambiguous", upsert("paid_failure"))

    def test_hold_revision_is_internal_and_never_reused_after_replace(self):
        hold = self.generation_hold("paid_failure")
        self.database.replace_ai_generation_holds([hold])
        first = self.database.ai_generation_hold(
            hold["hold_key"],
            include_revision=True,
        )
        self.assertNotIn(
            "revision",
            self.database.ai_generation_hold(hold["hold_key"]),
        )

        self.assertTrue(self.database.clear_ai_generation_hold(hold["hold_key"]))
        self.database.replace_ai_generation_holds([hold])
        second = self.database.ai_generation_hold(
            hold["hold_key"],
            include_revision=True,
        )
        self.assertGreater(second["revision"], first["revision"])

    def test_mark_sent_and_provisional_hold_are_atomic_and_settleable(self):
        job = self.job("atomic-provisional-hold")
        attempt = self.reserve(int(job["id"]), "atomic-provisional-hold-key")
        ambiguous = self.generation_hold(
            "ambiguous", include_timestamps=False
        )
        invalid = {**ambiguous, "hold_class": "invalid"}

        with self.assertRaisesRegex(ValueError, "hold class is invalid"):
            self.database.mark_ai_attempt_sent(
                int(attempt["id"]),
                provisional_generation_hold=invalid,
            )

        current_attempt = next(
            row
            for row in self.database.list_ai_attempts()
            if int(row["id"]) == int(attempt["id"])
        )
        self.assertEqual("reserved", current_attempt["state"])
        self.assertEqual("reserved", self.database.ai_job(int(job["id"]))["state"])
        self.assertEqual([], self.database.list_ai_generation_holds())

        created = self.database.mark_ai_attempt_sent(
            int(attempt["id"]),
            provisional_generation_hold=ambiguous,
        )
        self.assertTrue(created)
        current_attempt = next(
            row
            for row in self.database.list_ai_attempts()
            if int(row["id"]) == int(attempt["id"])
        )
        self.assertEqual("sent", current_attempt["state"])
        self.assertEqual("sent", self.database.ai_job(int(job["id"]))["state"])
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(ambiguous["hold_key"])[
                "hold_class"
            ],
        )

        self.database.fail_ai_attempt(
            attempt_id=int(attempt["id"]),
            job_state="permanent_failed",
            error_class="provider_http",
            error_code="http_429",
            error_message="fixture",
            generation_hold={
                **ambiguous,
                "hold_class": "fallback_pending",
            },
            settle_provisional_generation_hold=True,
        )
        self.assertEqual(
            "fallback_pending",
            self.database.ai_generation_hold(ambiguous["hold_key"])[
                "hold_class"
            ],
        )

    def test_preexisting_high_risk_hold_cannot_be_settled_down(self):
        existing = self.generation_hold("ambiguous")
        self.database.replace_ai_generation_holds([existing])
        job = self.job("forced-preexisting-hold")
        attempt = self.reserve(int(job["id"]), "forced-preexisting-hold-key")
        provisional = self.generation_hold(
            "ambiguous", include_timestamps=False
        )

        created = self.database.mark_ai_attempt_sent(
            int(attempt["id"]),
            provisional_generation_hold=provisional,
        )
        self.assertFalse(created)
        self.database.fail_ai_attempt(
            attempt_id=int(attempt["id"]),
            job_state="permanent_failed",
            error_class="provider_http",
            error_code="http_429",
            error_message="fixture",
            generation_hold={
                **provisional,
                "hold_class": "fallback_pending",
            },
            settle_provisional_generation_hold=created,
        )
        self.assertEqual(
            "ambiguous",
            self.database.ai_generation_hold(existing["hold_key"])[
                "hold_class"
            ],
        )

    def test_budget_reservation_is_atomic_across_competing_threads(self):
        jobs = [self.job("artifact-%d" % index) for index in range(2)]
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def run(index):
            barrier.wait()
            try:
                self.reserve(int(jobs[index]["id"]), "attempt-%d" % index, total_cap=10)
                outcome = "reserved"
            except AIBudgetExceeded:
                outcome = "blocked"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(["blocked", "reserved"], sorted(outcomes))
        self.assertEqual(1, len(self.database.list_ai_attempts()))

    def test_imported_usage_ledger_is_included_in_budget_reservation(self):
        self.database.replace_ai_usage_ledger(
            [
                self.usage_entry(
                    total_tokens=995,
                    input_tokens=995,
                    cache_miss_input_tokens=995,
                    output_tokens=0,
                )
            ]
        )
        job = self.job("carried-budget")
        with self.assertRaisesRegex(AIBudgetExceeded, "daily token budget exhausted"):
            self.database.reserve_ai_attempt(
                job_id=int(job["id"]),
                idempotency_key="carried-budget-key",
                requested_model="test-model",
                estimated_input_tokens=6,
                reserved_output_tokens=4,
                reserved_cost_micros=0,
                price_snapshot={},
                daily_started_at="2020-01-01T08:00:00Z",
                monthly_started_at="2020-01-01T08:00:00Z",
                daily_reset_at="2020-01-02T08:00:00Z",
                monthly_reset_at="2020-02-01T08:00:00Z",
                daily_max_requests=100,
                daily_max_total_tokens=1000,
                daily_max_cost_micros=0,
                monthly_max_requests=100,
                monthly_max_total_tokens=1000,
                monthly_max_cost_micros=0,
            )
        self.assertEqual(0, len(self.database.list_ai_attempts()))

    def test_usage_ledger_suppresses_only_local_attempts_it_covers(self):
        job = self.job("ledger-idempotence")
        attempt = self.reserve(int(job["id"]), "ledger-idempotence-key")
        started = datetime.fromisoformat(
            str(attempt["request_started_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        day_start = started.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        def iso(value):
            return value.isoformat().replace("+00:00", "Z")

        self.database.replace_ai_usage_ledger(
            [
                self.usage_entry(
                    timezone="UTC",
                    day_start=iso(day_start),
                    day_end=iso(day_end),
                    covered_through=iso(day_end),
                    confirmed_requests=0,
                    unconfirmed_requests=1,
                    input_tokens=0,
                    cache_miss_input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    reserved_total_tokens_for_unconfirmed=10,
                )
            ]
        )
        status = self.database.ai_status(iso(day_start), iso(day_start))
        self.assertEqual(
            {"requests": 1, "total_tokens": 10, "cost_micros": 0},
            status["daily"],
        )

        later = iso(day_end + timedelta(seconds=1))
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_attempts SET request_started_at=? WHERE id=?",
                (later, int(attempt["id"])),
            )
        status = self.database.ai_status(iso(day_start), iso(day_start))
        self.assertEqual(
            {"requests": 2, "total_tokens": 20, "cost_micros": 0},
            status["daily"],
        )

    def test_usage_ledger_replace_is_strict_atomic_and_attempt_rows_are_public_safe(self):
        valid = self.usage_entry()
        self.database.replace_ai_usage_ledger([valid])
        invalid = dict(valid)
        invalid["secret"] = "must not be accepted"
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self.database.replace_ai_usage_ledger([invalid])
        self.assertEqual([valid], self.database.list_ai_usage_ledger())

        attempt = self.reserve(int(self.job("safe-rollup")["id"]), "private-key")
        rows = self.database.list_ai_usage_attempt_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(str(attempt["request_started_at"]), rows[0]["request_started_at"])
        self.assertTrue(
            {"id", "idempotency_key", "provider_request_id", "error_message"}.isdisjoint(
                rows[0]
            )
        )

    def test_stalled_sent_attempt_becomes_unknown_and_is_never_retried(self):
        job = self.job("unknown-artifact")
        attempt = self.reserve(int(job["id"]), "unknown-attempt")
        self.database.mark_ai_attempt_sent(
            int(attempt["id"]),
            provisional_generation_hold=self.generation_hold(
                "ambiguous", include_timestamps=False
            ),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_attempts SET request_started_at='2000-01-01T00:00:00Z' WHERE id=?",
                (int(attempt["id"]),),
            )
        recovered = self.database.recover_stalled_ai_jobs(older_than_seconds=60)
        self.assertEqual(1, recovered["marked_unknown"])
        self.assertEqual("unknown", self.database.ai_job(int(job["id"]))["state"])
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("unknown", audit["state"])
        self.assertEqual(1, audit["reservation_active"])
        self.assertIsNone(self.database.lease_ai_job("worker"))

    def test_stalled_pre_send_reservation_is_safely_released(self):
        job = self.job("reserved-artifact")
        attempt = self.reserve(int(job["id"]), "reserved-attempt")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_attempts SET request_started_at='2000-01-01T00:00:00Z' WHERE id=?",
                (int(attempt["id"]),),
            )
        recovered = self.database.recover_stalled_ai_jobs(older_than_seconds=60)
        self.assertEqual(1, recovered["released_before_send"])
        audit = self.database.list_ai_attempts()[0]
        self.assertEqual("failed", audit["state"])
        self.assertEqual(0, audit["reservation_active"])
        self.assertEqual("retryable", self.database.ai_job(int(job["id"]))["state"])

    def test_provider_key_is_audited_but_is_not_attempt_identity(self):
        job = self.job("http-replay")
        first = self.reserve(int(job["id"]), "stable-provider-key")
        self.database.mark_ai_attempt_sent(
            int(first["id"]),
            provisional_generation_hold=self.generation_hold(
                "ambiguous", include_timestamps=False
            ),
        )
        self.database.fail_ai_attempt(
            attempt_id=int(first["id"]),
            job_state="retryable",
            error_class="provider_http",
            error_code="http_503",
            error_message="temporary provider failure",
            http_status=503,
        )
        second = self.reserve(int(job["id"]), "stable-provider-key")
        self.assertEqual(2, int(second["attempt_number"]))
        attempts = self.database.list_ai_attempts()
        self.assertEqual(2, len(attempts))
        self.assertEqual(
            ["stable-provider-key", "stable-provider-key"],
            [attempt["idempotency_key"] for attempt in attempts],
        )

    def test_client_request_id_cannot_be_rebound_to_a_different_request(self):
        first = self.database.ensure_ai_job(
            artifact_key="first-artifact",
            article_id=None,
            task_type="summary",
            input_scope="metadata",
            target_language="zh-CN",
            request={"version": 1, "key": "first"},
            client_request_id="same-client-request",
        )
        repeated = self.database.ensure_ai_job(
            artifact_key="first-artifact",
            article_id=None,
            task_type="summary",
            input_scope="metadata",
            target_language="zh-CN",
            request={"version": 1, "key": "first"},
            client_request_id="same-client-request",
        )
        self.assertEqual(first["id"], repeated["id"])
        with self.assertRaises(AIJobConflict):
            self.database.ensure_ai_job(
                artifact_key="different-artifact",
                article_id=None,
                task_type="summary",
                input_scope="metadata",
                target_language="zh-CN",
                request={"version": 1, "key": "different"},
                client_request_id="same-client-request",
            )

    def test_impossible_and_window_blocked_jobs_do_not_starve_the_queue(self):
        impossible = self.job("impossible-first")
        queued = self.job("queued-after-impossible")
        with self.assertRaises(AIBudgetExceeded):
            self.reserve(int(impossible["id"]), "impossible-key", total_cap=1)
        self.assertEqual(
            "permanent_failed", self.database.ai_job(int(impossible["id"]))["state"]
        )
        lease = self.database.lease_ai_job("worker-after-impossible")
        self.assertEqual(int(queued["id"]), int(lease["id"]))

        occupied = self.job("occupied-budget")
        self.reserve(int(occupied["id"]), "occupied-key", total_cap=15)
        blocked = self.job("blocked-until-reset")
        with self.assertRaises(AIBudgetExceeded):
            self.reserve(int(blocked["id"]), "blocked-key", total_cap=15)
        blocked_state = self.database.ai_job(int(blocked["id"]))
        self.assertEqual("budget_blocked", blocked_state["state"])
        self.assertEqual("2999-02-01T00:00:00Z", blocked_state["next_attempt_at"])
        queued_after_block = self.job("queued-after-window-block")
        lease_after_block = self.database.lease_ai_job("worker-after-window-block")
        self.assertEqual(int(queued_after_block["id"]), int(lease_after_block["id"]))


if __name__ == "__main__":
    unittest.main()
