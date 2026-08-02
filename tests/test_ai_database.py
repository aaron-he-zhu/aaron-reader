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
            task_type="digest",
            input_scope="digest",
            target_language="zh-CN",
            request={"version": 1, "key": key},
        )

    def reserve(self, job_id, key, total_cap=1000):
        return self.database.reserve_ai_attempt(
            job_id=job_id,
            idempotency_key=key,
            requested_model="test-model",
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
        self.database.mark_ai_attempt_sent(int(attempt["id"]))
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
        self.database.mark_ai_attempt_sent(int(first["id"]))
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
            task_type="digest",
            input_scope="digest",
            target_language="zh-CN",
            request={"version": 1, "key": "first"},
            client_request_id="same-client-request",
        )
        repeated = self.database.ensure_ai_job(
            artifact_key="first-artifact",
            article_id=None,
            task_type="digest",
            input_scope="digest",
            target_language="zh-CN",
            request={"version": 1, "key": "first"},
            client_request_id="same-client-request",
        )
        self.assertEqual(first["id"], repeated["id"])
        with self.assertRaises(AIJobConflict):
            self.database.ensure_ai_job(
                artifact_key="different-artifact",
                article_id=None,
                task_type="digest",
                input_scope="digest",
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
