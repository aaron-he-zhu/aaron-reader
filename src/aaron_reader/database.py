import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

from .i18n import translate
from .models import ArticleCandidate, SourceConfig


SCHEMA_VERSION = 8


class AIBudgetExceeded(ValueError):
    """Raised before a provider call when a configured hard cap is exhausted."""


class AIJobConflict(ValueError):
    """Raised when a job cannot safely transition to the requested state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            stored_version = 0
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
            ).fetchone()
            if has_meta:
                row = connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()
                if row is not None:
                    try:
                        stored_version = int(row[0])
                    except (TypeError, ValueError) as exc:
                        raise ValueError("invalid database schema_version") from exc
                    if stored_version > SCHEMA_VERSION:
                        raise ValueError(
                            "database schema version %d is newer than supported version %d"
                            % (stored_version, SCHEMA_VERSION)
                        )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    home_url TEXT NOT NULL,
                    fetch_url TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    body_hash TEXT NOT NULL DEFAULT '',
                    initialized_at TEXT,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_http_status INTEGER,
                    last_item_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_slug TEXT NOT NULL REFERENCES sources(slug),
                    external_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    published_at TEXT,
                    modified_at TEXT,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    is_backfill INTEGER NOT NULL DEFAULT 0,
                    read_at TEXT,
                    starred_at TEXT,
                    UNIQUE(source_slug, external_id),
                    UNIQUE(source_slug, canonical_url)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_sort
                    ON articles(published_at DESC, discovered_at DESC);
                CREATE INDEX IF NOT EXISTS idx_articles_unread
                    ON articles(read_at, published_at DESC);

                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_slug TEXT NOT NULL REFERENCES sources(slug),
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    updated INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS http_cache (
                    url TEXT PRIMARY KEY,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    body_hash TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL,
                    last_status INTEGER
                );

                CREATE TABLE IF NOT EXISTS seen_urls (
                    source_slug TEXT NOT NULL REFERENCES sources(slug),
                    url TEXT NOT NULL,
                    remote_modified TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(source_slug, url)
                );

                CREATE TABLE IF NOT EXISTS pending_urls (
                    source_slug TEXT NOT NULL REFERENCES sources(slug),
                    url TEXT NOT NULL,
                    remote_modified TEXT NOT NULL DEFAULT '',
                    change_kind TEXT NOT NULL CHECK(change_kind IN ('new', 'modified')),
                    first_seen_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    next_attempt_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source_slug, url)
                );

                CREATE TABLE IF NOT EXISTS source_checks (
                    source_slug TEXT NOT NULL REFERENCES sources(slug),
                    check_name TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    last_success_at TEXT,
                    failure_streak INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(source_slug, check_name)
                );

                CREATE TABLE IF NOT EXISTS article_content_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    canonical_url TEXT NOT NULL,
                    final_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    extractor_version TEXT NOT NULL,
                    normalized_text_hash TEXT NOT NULL,
                    character_count INTEGER NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
                    normalized_text TEXT NOT NULL,
                    UNIQUE(article_id, normalized_text_hash, extractor_version)
                );
                CREATE INDEX IF NOT EXISTS idx_content_snapshots_article
                    ON article_content_snapshots(article_id, retrieved_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS ai_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    task_type TEXT NOT NULL CHECK(task_type IN ('summary', 'translation', 'digest')),
                    input_scope TEXT NOT NULL CHECK(input_scope IN ('metadata', 'full_text', 'digest')),
                    source_language TEXT NOT NULL DEFAULT '',
                    target_language TEXT NOT NULL,
                    artifact_key TEXT NOT NULL UNIQUE,
                    input_hash TEXT NOT NULL,
                    article_content_hash TEXT NOT NULL DEFAULT '',
                    source_artifact_id INTEGER REFERENCES ai_artifacts(id) ON DELETE SET NULL,
                    content_snapshot_id INTEGER REFERENCES article_content_snapshots(id) ON DELETE SET NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    response_schema_version TEXT NOT NULL,
                    response_schema_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    resolved_model TEXT NOT NULL,
                    generation_params_hash TEXT NOT NULL,
                    provider_response_id TEXT NOT NULL DEFAULT '',
                    output_json TEXT NOT NULL,
                    output_text TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('succeeded', 'stale')),
                    input_truncated INTEGER NOT NULL DEFAULT 0 CHECK(input_truncated IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_artifacts_article
                    ON ai_artifacts(article_id, task_type, target_language, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_artifacts_digest
                    ON ai_artifacts(task_type, target_language, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS ai_reports (
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
                CREATE INDEX IF NOT EXISTS idx_ai_reports_latest
                    ON ai_reports(period, target_language, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS ai_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artifact_key TEXT NOT NULL UNIQUE,
                    article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    task_type TEXT NOT NULL CHECK(task_type IN ('summary', 'translation', 'digest')),
                    input_scope TEXT NOT NULL CHECK(input_scope IN ('metadata', 'full_text', 'digest')),
                    target_language TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    trigger_kind TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'queued', 'leased', 'reserved', 'sent', 'succeeded', 'retryable',
                        'unknown', 'budget_blocked', 'permanent_failed', 'cancelled'
                    )),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    lease_owner TEXT,
                    lease_until TEXT,
                    next_attempt_at TEXT,
                    artifact_id INTEGER REFERENCES ai_artifacts(id) ON DELETE SET NULL,
                    client_request_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ai_jobs_queue
                    ON ai_jobs(state, priority, next_attempt_at, created_at, id);

                CREATE TABLE IF NOT EXISTS ai_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES ai_jobs(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved', 'sent', 'succeeded', 'failed', 'unknown')),
                    request_started_at TEXT NOT NULL,
                    response_received_at TEXT,
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    requested_provider TEXT NOT NULL DEFAULT 'unknown',
                    requested_model TEXT NOT NULL,
                    resolved_model TEXT NOT NULL DEFAULT '',
                    estimated_input_tokens INTEGER NOT NULL,
                    reserved_output_tokens INTEGER NOT NULL,
                    reserved_total_tokens INTEGER NOT NULL,
                    actual_input_tokens INTEGER,
                    actual_cached_input_tokens INTEGER,
                    actual_cache_write_tokens INTEGER,
                    actual_output_tokens INTEGER,
                    actual_reasoning_tokens INTEGER,
                    actual_total_tokens INTEGER,
                    reserved_cost_micros INTEGER NOT NULL DEFAULT 0,
                    actual_cost_micros INTEGER,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    price_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    reservation_active INTEGER NOT NULL DEFAULT 1 CHECK(reservation_active IN (0, 1)),
                    http_status INTEGER,
                    finish_reason TEXT NOT NULL DEFAULT '',
                    error_class TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    response_hash TEXT NOT NULL DEFAULT '',
                    UNIQUE(job_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_attempts_started
                    ON ai_attempts(request_started_at, state);

                CREATE TABLE IF NOT EXISTS ai_usage_ledger (
                    timezone TEXT NOT NULL,
                    day_start TEXT PRIMARY KEY,
                    day_end TEXT NOT NULL,
                    covered_through TEXT NOT NULL,
                    requests INTEGER NOT NULL CHECK(requests >= 0),
                    confirmed_requests INTEGER NOT NULL CHECK(confirmed_requests >= 0),
                    unconfirmed_requests INTEGER NOT NULL CHECK(unconfirmed_requests >= 0),
                    input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
                    cached_input_tokens INTEGER NOT NULL CHECK(cached_input_tokens >= 0),
                    cache_miss_input_tokens INTEGER NOT NULL CHECK(cache_miss_input_tokens >= 0),
                    cache_write_input_tokens INTEGER NOT NULL CHECK(cache_write_input_tokens >= 0),
                    output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
                    reasoning_tokens INTEGER NOT NULL CHECK(reasoning_tokens >= 0),
                    total_tokens INTEGER NOT NULL CHECK(total_tokens >= 0),
                    reserved_total_tokens_for_unconfirmed INTEGER NOT NULL
                        CHECK(reserved_total_tokens_for_unconfirmed >= 0),
                    cost_micros INTEGER NOT NULL CHECK(cost_micros >= 0),
                    reserved_cost_micros_for_unconfirmed INTEGER NOT NULL
                        CHECK(reserved_cost_micros_for_unconfirmed >= 0),
                    CHECK(day_start < day_end),
                    CHECK(day_start <= covered_through),
                    CHECK(confirmed_requests + unconfirmed_requests = requests)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_usage_ledger_end
                    ON ai_usage_ledger(day_end);

                CREATE TABLE IF NOT EXISTS ai_generation_holds (
                    hold_key TEXT PRIMARY KEY,
                    workload_kind TEXT NOT NULL CHECK(workload_kind IN (
                        'article', 'article_pair', 'digest', 'report'
                    )),
                    hold_class TEXT NOT NULL CHECK(hold_class IN (
                        'ambiguous', 'paid_failure', 'fallback_pending'
                    )),
                    descriptor_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                    CHECK(length(hold_key) = 64),
                    CHECK(first_seen_at <= last_seen_at)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_generation_holds_seen
                    ON ai_generation_holds(last_seen_at DESC);
                """
            )
            if stored_version < 4:
                self._migrate_ai_attempts_v4(connection)
            if stored_version < 7:
                self._migrate_ai_schema_v7(connection)
            if stored_version < 8:
                self._migrate_ai_schema_v8(connection)
            pending_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(pending_urls)").fetchall()
            }
            if "next_attempt_at" not in pending_columns:
                connection.execute("ALTER TABLE pending_urls ADD COLUMN next_attempt_at TEXT")
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate_ai_attempts_v4(connection: sqlite3.Connection) -> None:
        """Remove the obsolete global idempotency-key uniqueness constraint.

        Attempt identity is ``(job_id, attempt_number)``.  Provider keys remain
        audited, but the database must not assume an undocumented provider-side
        replay contract.  Rebuilding the table is the only portable SQLite way
        to remove the inline UNIQUE constraint.
        """

        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_attempts'"
        ).fetchone()
        table_sql = str(table_sql_row[0] or "") if table_sql_row else ""
        normalized_table_sql = " ".join(table_sql.split()).lower()
        if "idempotency_key text not null unique" not in normalized_table_sql:
            return
        connection.execute("DROP TABLE IF EXISTS ai_attempts_v4")
        connection.execute(
            """
            CREATE TABLE ai_attempts_v4 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES ai_jobs(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved', 'sent', 'succeeded', 'failed', 'unknown')),
                request_started_at TEXT NOT NULL,
                response_received_at TEXT,
                provider_request_id TEXT NOT NULL DEFAULT '',
                requested_model TEXT NOT NULL,
                resolved_model TEXT NOT NULL DEFAULT '',
                estimated_input_tokens INTEGER NOT NULL,
                reserved_output_tokens INTEGER NOT NULL,
                reserved_total_tokens INTEGER NOT NULL,
                actual_input_tokens INTEGER,
                actual_cached_input_tokens INTEGER,
                actual_cache_write_tokens INTEGER,
                actual_output_tokens INTEGER,
                actual_reasoning_tokens INTEGER,
                actual_total_tokens INTEGER,
                reserved_cost_micros INTEGER NOT NULL DEFAULT 0,
                actual_cost_micros INTEGER,
                currency TEXT NOT NULL DEFAULT 'USD',
                price_snapshot_json TEXT NOT NULL DEFAULT '{}',
                reservation_active INTEGER NOT NULL DEFAULT 1 CHECK(reservation_active IN (0, 1)),
                http_status INTEGER,
                finish_reason TEXT NOT NULL DEFAULT '',
                error_class TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                response_hash TEXT NOT NULL DEFAULT '',
                UNIQUE(job_id, attempt_number)
            )
            """
        )
        columns = (
            "id, job_id, attempt_number, idempotency_key, state, request_started_at, "
            "response_received_at, provider_request_id, requested_model, resolved_model, "
            "estimated_input_tokens, reserved_output_tokens, reserved_total_tokens, "
            "actual_input_tokens, actual_cached_input_tokens, actual_cache_write_tokens, "
            "actual_output_tokens, actual_reasoning_tokens, actual_total_tokens, "
            "reserved_cost_micros, actual_cost_micros, currency, price_snapshot_json, "
            "reservation_active, http_status, finish_reason, error_class, error_code, "
            "error_message, response_hash"
        )
        connection.execute(
            "INSERT INTO ai_attempts_v4(%s) SELECT %s FROM ai_attempts" % (columns, columns)
        )
        connection.execute("DROP TABLE ai_attempts")
        connection.execute("ALTER TABLE ai_attempts_v4 RENAME TO ai_attempts")
        connection.execute(
            "CREATE INDEX idx_ai_attempts_started ON ai_attempts(request_started_at, state)"
        )

    @staticmethod
    def _migrate_ai_schema_v7(connection: sqlite3.Connection) -> None:
        """Add explicit provider audit and the crash-safe fallback hold class."""

        attempt_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(ai_attempts)").fetchall()
        }
        if "requested_provider" not in attempt_columns:
            connection.execute(
                "ALTER TABLE ai_attempts ADD COLUMN "
                "requested_provider TEXT NOT NULL DEFAULT 'unknown'"
            )
            connection.execute(
                """
                UPDATE ai_attempts
                SET requested_provider = CASE requested_model
                    WHEN 'openrouter/free' THEN 'openrouter'
                    WHEN 'deepseek-v4-flash' THEN 'deepseek'
                    ELSE 'unknown'
                END
                """
            )

        table_row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='ai_generation_holds'"
        ).fetchone()
        table_sql = str(table_row[0] or "") if table_row else ""
        if "fallback_pending" in table_sql:
            return
        connection.execute("DROP TABLE IF EXISTS ai_generation_holds_v7")
        connection.execute(
            """
            CREATE TABLE ai_generation_holds_v7 (
                hold_key TEXT PRIMARY KEY,
                workload_kind TEXT NOT NULL CHECK(workload_kind IN (
                    'article', 'article_pair', 'digest', 'report'
                )),
                hold_class TEXT NOT NULL CHECK(hold_class IN (
                    'ambiguous', 'paid_failure', 'fallback_pending'
                )),
                descriptor_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                CHECK(length(hold_key) = 64),
                CHECK(first_seen_at <= last_seen_at)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ai_generation_holds_v7(
                hold_key, workload_kind, hold_class, descriptor_json,
                first_seen_at, last_seen_at
            )
            SELECT hold_key, workload_kind, hold_class, descriptor_json,
                first_seen_at, last_seen_at
            FROM ai_generation_holds
            """
        )
        connection.execute("DROP TABLE ai_generation_holds")
        connection.execute(
            "ALTER TABLE ai_generation_holds_v7 RENAME TO ai_generation_holds"
        )
        connection.execute(
            "CREATE INDEX idx_ai_generation_holds_seen "
            "ON ai_generation_holds(last_seen_at DESC)"
        )

    @staticmethod
    def _migrate_ai_schema_v8(connection: sqlite3.Connection) -> None:
        """Add an internal monotonic revision for hold snapshot CAS."""

        hold_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(ai_generation_holds)"
            ).fetchall()
        }
        if "revision" not in hold_columns:
            connection.execute(
                "ALTER TABLE ai_generation_holds ADD COLUMN "
                "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)"
            )
        Database._ensure_ai_generation_hold_revision_sequence(connection)

    def sync_source_configs(
        self, sources: Iterable[SourceConfig], language: str = "en"
    ) -> None:
        configured = list(sources)
        now = utc_now()
        with self.connect() as connection:
            for source in configured:
                existing = connection.execute(
                    "SELECT home_url, fetch_url, adapter, initialized_at FROM sources WHERE slug=?",
                    (source.slug,),
                ).fetchone()
                if existing is not None and existing["initialized_at"]:
                    identity_changed = any(
                        (
                            existing["home_url"] != source.home_url,
                            existing["fetch_url"] != source.fetch_url,
                            existing["adapter"] != source.adapter,
                        )
                    )
                    if identity_changed:
                        raise ValueError(
                            translate(
                                "database.source_identity_changed",
                                language,
                                source=source.slug,
                            )
                        )
            for source in configured:
                connection.execute(
                    """
                    INSERT INTO sources(
                        slug, name, home_url, fetch_url, adapter, enabled, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name=excluded.name,
                        home_url=excluded.home_url,
                        etag=CASE WHEN sources.fetch_url != excluded.fetch_url THEN '' ELSE sources.etag END,
                        last_modified=CASE WHEN sources.fetch_url != excluded.fetch_url THEN '' ELSE sources.last_modified END,
                        body_hash=CASE WHEN sources.fetch_url != excluded.fetch_url THEN '' ELSE sources.body_hash END,
                        fetch_url=excluded.fetch_url,
                        adapter=excluded.adapter,
                        enabled=excluded.enabled,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source.slug,
                        source.name,
                        source.home_url,
                        source.fetch_url,
                        source.adapter,
                        int(source.enabled),
                        now,
                    ),
                )

    def source_state(self, slug: str) -> Dict[str, object]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sources WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise KeyError("unknown source: %s" % slug)
        return dict(row)

    def http_cache(self, url: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def record_http_cache(
        self,
        url: str,
        status: int,
        etag: str = "",
        last_modified: str = "",
        body_hash: str = "",
        not_modified: bool = False,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO http_cache(url, etag, last_modified, body_hash, last_checked_at, last_status)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag=CASE WHEN ? THEN COALESCE(NULLIF(excluded.etag, ''), http_cache.etag) ELSE excluded.etag END,
                    last_modified=CASE WHEN ? THEN COALESCE(NULLIF(excluded.last_modified, ''), http_cache.last_modified) ELSE excluded.last_modified END,
                    body_hash=CASE WHEN ? THEN COALESCE(NULLIF(excluded.body_hash, ''), http_cache.body_hash) ELSE excluded.body_hash END,
                    last_checked_at=excluded.last_checked_at,
                    last_status=excluded.last_status
                """,
                (
                    url,
                    etag,
                    last_modified,
                    body_hash,
                    now,
                    status,
                    int(not_modified),
                    int(not_modified),
                    int(not_modified),
                ),
            )

    def sitemap_is_due(self, url: str, interval_hours: int) -> bool:
        cache = self.http_cache(url)
        if not cache or not cache.get("last_checked_at"):
            return True
        try:
            checked = datetime.fromisoformat(str(cache["last_checked_at"]).replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - checked.astimezone(timezone.utc)
            return age.total_seconds() >= interval_hours * 3600
        except ValueError:
            return True

    def discover_sitemap_urls(
        self,
        source_slug: str,
        entries: Sequence[Tuple[str, Optional[str]]],
    ) -> Tuple[bool, List[Tuple[str, Optional[str]]]]:
        now = utc_now()
        with self.connect() as connection:
            known_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM seen_urls WHERE source_slug=?", (source_slug,)
                ).fetchone()[0]
            )
            baseline = known_count == 0
            if baseline:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO seen_urls(
                        source_slug, url, remote_modified, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (source_slug, url, modified or "", now, now)
                        for url, modified in entries
                    ],
                )
                return True, []

            new_entries: List[Tuple[str, Optional[str]]] = []
            for url, modified in entries:
                row = connection.execute(
                    "SELECT remote_modified FROM seen_urls WHERE source_slug=? AND url=?",
                    (source_slug, url),
                ).fetchone()
                if row is None:
                    new_entries.append((url, modified))
                    connection.execute(
                        """
                        INSERT INTO pending_urls(
                            source_slug, url, remote_modified, change_kind, first_seen_at
                        ) VALUES (?, ?, ?, 'new', ?)
                        ON CONFLICT(source_slug, url) DO UPDATE SET
                            remote_modified=excluded.remote_modified
                        """,
                        (source_slug, url, modified or "", now),
                    )
                    continue
                old_modified = str(row["remote_modified"] or "")
                new_modified = modified or ""
                if old_modified and new_modified and old_modified != new_modified:
                    new_entries.append((url, modified))
                    connection.execute(
                        """
                        INSERT INTO pending_urls(
                            source_slug, url, remote_modified, change_kind, first_seen_at
                        ) VALUES (?, ?, ?, 'modified', ?)
                        ON CONFLICT(source_slug, url) DO UPDATE SET
                            remote_modified=excluded.remote_modified,
                            change_kind=CASE
                                WHEN pending_urls.change_kind='new' THEN 'new'
                                ELSE 'modified'
                            END
                        """,
                        (source_slug, url, new_modified, now),
                    )
                    connection.execute(
                        "UPDATE seen_urls SET last_seen_at=? WHERE source_slug=? AND url=?",
                        (now, source_slug, url),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE seen_urls SET
                            remote_modified=CASE
                                WHEN remote_modified='' THEN ? ELSE remote_modified
                            END,
                            last_seen_at=?
                        WHERE source_slug=? AND url=?
                        """,
                        (new_modified, now, source_slug, url),
                    )
        return False, new_entries

    def pending_url_count(self, source_slug: str) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM pending_urls WHERE source_slug=?",
                    (source_slug,),
                ).fetchone()[0]
            )

    def pending_urls(self, source_slug: str, limit: int) -> List[Dict[str, object]]:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pending_urls
                WHERE source_slug=?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY CASE change_kind WHEN 'new' THEN 0 ELSE 1 END,
                         first_seen_at, url
                LIMIT ?
                """,
                (source_slug, now, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_pending_failure(
        self,
        source_slug: str,
        url: str,
        error: str,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM pending_urls WHERE source_slug=? AND url=?",
                (source_slug, url),
            ).fetchone()
            attempts = int(row["attempt_count"] or 0) + 1 if row else 1
            if retry_after_seconds is None:
                delay = min(6 * 3600, 60 * (2 ** min(attempts - 1, 8)))
            else:
                delay = max(0.0, min(float(retry_after_seconds), 24 * 3600))
            next_attempt = (now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            connection.execute(
                """
                UPDATE pending_urls SET
                    last_attempt_at=?, next_attempt_at=?,
                    attempt_count=attempt_count+1, last_error=?
                WHERE source_slug=? AND url=?
                """,
                (
                    now.isoformat().replace("+00:00", "Z"),
                    next_attempt,
                    error[:2000],
                    source_slug,
                    url,
                ),
            )

    def record_check_success(self, source_slug: str, check_name: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_checks(
                    source_slug, check_name, last_checked_at, last_success_at,
                    failure_streak, last_error
                ) VALUES (?, ?, ?, ?, 0, '')
                ON CONFLICT(source_slug, check_name) DO UPDATE SET
                    last_checked_at=excluded.last_checked_at,
                    last_success_at=excluded.last_success_at,
                    failure_streak=0,
                    last_error=''
                """,
                (source_slug, check_name, now, now),
            )

    def record_check_failure(self, source_slug: str, check_name: str, error: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_checks(
                    source_slug, check_name, last_checked_at, failure_streak, last_error
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(source_slug, check_name) DO UPDATE SET
                    last_checked_at=excluded.last_checked_at,
                    failure_streak=source_checks.failure_streak+1,
                    last_error=excluded.last_error
                """,
                (source_slug, check_name, now, error[:2000]),
            )

    def mark_seen_urls(
        self,
        source_slug: str,
        entries: Sequence[Tuple[str, Optional[str]]],
    ) -> None:
        if not entries:
            return
        now = utc_now()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO seen_urls(source_slug, url, remote_modified, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_slug, url) DO UPDATE SET
                    remote_modified=excluded.remote_modified,
                    last_seen_at=excluded.last_seen_at
                """,
                [(source_slug, url, modified or "", now, now) for url, modified in entries],
            )
            connection.executemany(
                "DELETE FROM pending_urls WHERE source_slug=? AND url=?",
                [(source_slug, url) for url, _ in entries],
            )

    def record_not_modified(
        self,
        slug: str,
        http_status: int = 304,
        etag: str = "",
        last_modified: str = "",
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources SET
                    etag=COALESCE(NULLIF(?, ''), etag),
                    last_modified=COALESCE(NULLIF(?, ''), last_modified),
                    last_checked_at=?, last_success_at=?, failure_streak=0,
                    last_error='', last_http_status=?, updated_at=?
                WHERE slug=?
                """,
                (etag, last_modified, now, now, http_status, now, slug),
            )
            self._insert_run(connection, slug, now, now, "not_modified", http_status, 0, 0, 0, "")

    def record_unchanged(
        self,
        slug: str,
        started_at: str,
        http_status: int,
        etag: str,
        last_modified: str,
        body_hash: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources SET
                    etag=?, last_modified=?, body_hash=?, last_checked_at=?,
                    last_success_at=?, failure_streak=0, last_error='',
                    last_http_status=?, updated_at=?
                WHERE slug=?
                """,
                (etag, last_modified, body_hash, now, now, http_status, now, slug),
            )
            self._insert_run(
                connection, slug, started_at, now, "unchanged", http_status, 0, 0, 0, ""
            )

    def record_failure(
        self,
        slug: str,
        started_at: str,
        error: str,
        http_status: Optional[int] = None,
    ) -> None:
        now = utc_now()
        safe_error = error[:2000]
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE sources SET
                    last_checked_at=?, failure_streak=failure_streak+1,
                    last_error=?, last_http_status=?, updated_at=?
                WHERE slug=?
                """,
                (now, safe_error, http_status, now, slug),
            )
            self._insert_run(
                connection, slug, started_at, now, "error", http_status, 0, 0, 0, safe_error
            )

    def commit_candidates(
        self,
        source: SourceConfig,
        candidates: Sequence[ArticleCandidate],
        started_at: str,
        http_status: int,
        etag: str,
        last_modified: str,
        body_hash: str,
        force_history_unread: bool = False,
        listing_item_count: Optional[int] = None,
    ) -> Tuple[int, int, int, bool, List[int]]:
        now = utc_now()
        inserted = 0
        updated = 0
        seeded = 0
        new_ids: List[int] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT initialized_at FROM sources WHERE slug=?", (source.slug,)
            ).fetchone()
            if state is None:
                raise KeyError("unknown source: %s" % source.slug)
            baseline = state["initialized_at"] is None

            for candidate in candidates:
                external_id = candidate.external_id or candidate.url
                existing = connection.execute(
                    """
                    SELECT * FROM articles
                    WHERE source_slug=? AND (external_id=? OR canonical_url=?)
                    ORDER BY id LIMIT 1
                    """,
                    (source.slug, external_id, candidate.url),
                ).fetchone()
                if existing is None:
                    is_backfill = int(baseline)
                    read_at = now if baseline and not force_history_unread else None
                    cursor = connection.execute(
                        """
                        INSERT INTO articles(
                            source_slug, external_id, canonical_url, title, summary,
                            author, category, published_at, modified_at, discovered_at,
                            updated_at, content_hash, is_backfill, read_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source.slug,
                            external_id,
                            candidate.url,
                            candidate.title,
                            candidate.summary,
                            candidate.author,
                            candidate.category,
                            candidate.published_at,
                            candidate.modified_at,
                            now,
                            now,
                            candidate.content_hash,
                            is_backfill,
                            read_at,
                        ),
                    )
                    inserted += 1
                    if baseline:
                        seeded += 1
                    else:
                        new_ids.append(int(cursor.lastrowid))
                    continue

                changed = any(
                    (
                        existing["external_id"] != external_id,
                        existing["canonical_url"] != candidate.url,
                        existing["title"] != candidate.title,
                        existing["summary"] != candidate.summary,
                        existing["author"] != candidate.author,
                        existing["category"] != candidate.category,
                        existing["published_at"] != candidate.published_at,
                        existing["modified_at"] != candidate.modified_at,
                        existing["content_hash"] != candidate.content_hash,
                    )
                )
                if changed:
                    connection.execute(
                        """
                        UPDATE articles SET
                            external_id=?, canonical_url=?, title=?, summary=?, author=?,
                            category=?, published_at=?, modified_at=?, updated_at=?, content_hash=?
                        WHERE id=?
                        """,
                        (
                            external_id,
                            candidate.url,
                            candidate.title,
                            candidate.summary,
                            candidate.author,
                            candidate.category,
                            candidate.published_at,
                            candidate.modified_at,
                            now,
                            candidate.content_hash,
                            existing["id"],
                        ),
                    )
                    updated += 1

            connection.execute(
                """
                UPDATE sources SET
                    etag=?, last_modified=?, body_hash=?,
                    initialized_at=COALESCE(initialized_at, ?),
                    last_checked_at=?, last_success_at=?, failure_streak=0,
                    last_error='', last_http_status=?,
                    last_item_count=CASE WHEN ? IS NULL THEN last_item_count ELSE ? END,
                    updated_at=?
                WHERE slug=?
                """,
                (
                    etag,
                    last_modified,
                    body_hash,
                    now,
                    now,
                    now,
                    http_status,
                    listing_item_count,
                    listing_item_count,
                    now,
                    source.slug,
                ),
            )
            self._insert_run(
                connection,
                source.slug,
                started_at,
                now,
                "ok",
                http_status,
                len(candidates),
                inserted,
                updated,
                "",
            )
        return inserted, updated, seeded, baseline, new_ids

    @staticmethod
    def _insert_run(
        connection: sqlite3.Connection,
        slug: str,
        started_at: str,
        finished_at: str,
        status: str,
        http_status: Optional[int],
        discovered: int,
        inserted: int,
        updated: int,
        error: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO sync_runs(
                source_slug, started_at, finished_at, status, http_status,
                discovered, inserted, updated, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                started_at,
                finished_at,
                status,
                http_status,
                discovered,
                inserted,
                updated,
                error,
            ),
        )

    def list_articles(
        self,
        limit: int = 100,
        source_slug: Optional[str] = None,
        unread_only: bool = False,
        starred_only: bool = False,
        query: str = "",
    ) -> List[Dict[str, object]]:
        clauses = []
        values: List[object] = []
        if source_slug:
            clauses.append("a.source_slug = ?")
            values.append(source_slug)
        if unread_only:
            clauses.append("a.read_at IS NULL")
        if starred_only:
            clauses.append("a.starred_at IS NOT NULL")
        if query:
            clauses.append("(a.title LIKE ? OR a.summary LIKE ? OR a.category LIKE ?)")
            term = "%%%s%%" % query
            values.extend((term, term, term))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        sql = """
            SELECT a.*, s.name AS source_name, s.home_url AS source_home_url
            FROM articles a JOIN sources s ON s.slug=a.source_slug
            %s
            ORDER BY COALESCE(a.published_at, a.discovered_at) DESC, a.id DESC
            LIMIT ?
        """ % where
        with self.connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def list_articles_between(
        self,
        period_start: str,
        period_end: str,
        *,
        limit: int = 1000,
    ) -> List[Dict[str, object]]:
        """Return articles published or discovered inside one UTC window.

        ``published_at`` remains the canonical timeline when present.  Entries
        without a publisher date fall back to their deterministic local
        discovery time, matching the reader's ordinary ordering semantics.
        """

        bounded_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, s.name AS source_name, s.home_url AS source_home_url
                FROM articles a JOIN sources s ON s.slug=a.source_slug
                WHERE julianday(COALESCE(a.published_at, a.discovered_at))
                          >= julianday(?)
                  AND julianday(COALESCE(a.published_at, a.discovered_at))
                          <= julianday(?)
                ORDER BY COALESCE(a.published_at, a.discovered_at) DESC,
                    a.source_slug ASC, a.external_id ASC, a.canonical_url ASC
                LIMIT ?
                """,
                (period_start, period_end, bounded_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def article(self, article_id: int) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, s.name AS source_name
                FROM articles a JOIN sources s ON s.slug=a.source_slug
                WHERE a.id=?
                """,
                (article_id,),
            ).fetchone()
        return dict(row) if row else None

    def article_by_url(self, source_slug: str, canonical_url: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE source_slug=? AND canonical_url=?",
                (source_slug, canonical_url),
            ).fetchone()
        return dict(row) if row else None

    def source_has_missing_dates(self, source_slug: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM articles WHERE source_slug=? AND published_at IS NULL LIMIT 1",
                (source_slug,),
            ).fetchone()
        return row is not None

    def set_read(self, article_ids: Sequence[int], read: bool = True) -> int:
        if not article_ids:
            return 0
        value = utc_now() if read else None
        placeholders = ",".join("?" for _ in article_ids)
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE articles SET read_at=? WHERE id IN (%s)" % placeholders,
                [value] + [int(item) for item in article_ids],
            )
        return cursor.rowcount

    def mark_all_read(self, source_slug: Optional[str] = None) -> int:
        now = utc_now()
        with self.connect() as connection:
            if source_slug:
                cursor = connection.execute(
                    "UPDATE articles SET read_at=? WHERE source_slug=? AND read_at IS NULL",
                    (now, source_slug),
                )
            else:
                cursor = connection.execute(
                    "UPDATE articles SET read_at=? WHERE read_at IS NULL", (now,)
                )
        return cursor.rowcount

    def mark_all_unread(self, source_slug: Optional[str] = None) -> int:
        with self.connect() as connection:
            if source_slug:
                cursor = connection.execute(
                    "UPDATE articles SET read_at=NULL WHERE source_slug=? AND read_at IS NOT NULL",
                    (source_slug,),
                )
            else:
                cursor = connection.execute(
                    "UPDATE articles SET read_at=NULL WHERE read_at IS NOT NULL"
                )
        return cursor.rowcount

    def set_starred(self, article_id: int, starred: bool = True) -> bool:
        value = utc_now() if starred else None
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE articles SET starred_at=? WHERE id=?", (value, article_id)
            )
        return cursor.rowcount > 0

    # AI enrichment is an optional sidecar.  These methods never construct a
    # provider client and are safe to use while AI is disabled.

    def store_content_snapshot(
        self,
        *,
        article_id: int,
        canonical_url: str,
        final_url: str,
        content_type: str,
        extractor_version: str,
        normalized_text_hash: str,
        normalized_text: str,
        truncated: bool,
        etag: str = "",
        last_modified: str = "",
        retrieved_at: Optional[str] = None,
    ) -> Dict[str, object]:
        now = retrieved_at or utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO article_content_snapshots(
                    article_id, canonical_url, final_url, retrieved_at, content_type,
                    etag, last_modified, extractor_version, normalized_text_hash,
                    character_count, truncated, normalized_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id, normalized_text_hash, extractor_version)
                DO UPDATE SET
                    canonical_url=excluded.canonical_url,
                    final_url=excluded.final_url,
                    retrieved_at=excluded.retrieved_at,
                    content_type=excluded.content_type,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    character_count=excluded.character_count,
                    truncated=excluded.truncated,
                    normalized_text=excluded.normalized_text
                """,
                (
                    int(article_id),
                    canonical_url,
                    final_url,
                    now,
                    content_type,
                    etag,
                    last_modified,
                    extractor_version,
                    normalized_text_hash,
                    len(normalized_text),
                    int(truncated),
                    normalized_text,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM article_content_snapshots
                WHERE article_id=? AND normalized_text_hash=? AND extractor_version=?
                """,
                (int(article_id), normalized_text_hash, extractor_version),
            ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("content snapshot was not stored")
        return dict(row)

    def latest_content_snapshot(self, article_id: int) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM article_content_snapshots
                WHERE article_id=?
                ORDER BY retrieved_at DESC, id DESC
                LIMIT 1
                """,
                (int(article_id),),
            ).fetchone()
        return dict(row) if row else None

    def ai_artifact_by_key(self, artifact_key: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_artifacts WHERE artifact_key=?",
                (artifact_key,),
            ).fetchone()
        return dict(row) if row else None

    def ai_artifact(self, artifact_id: int) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_artifacts WHERE id=?", (int(artifact_id),)
            ).fetchone()
        return dict(row) if row else None

    def latest_ai_artifacts(
        self, article_ids: Sequence[int]
    ) -> Dict[int, List[Dict[str, object]]]:
        if not article_ids:
            return {}
        normalized_ids = [int(value) for value in article_ids]
        placeholders = ",".join("?" for _ in normalized_ids)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT aa.*
                FROM ai_artifacts aa
                JOIN articles a ON a.id=aa.article_id
                WHERE aa.article_id IN (%s)
                  AND aa.status='succeeded'
                  AND aa.article_content_hash=a.content_hash
                ORDER BY aa.created_at DESC, aa.id DESC
                """ % placeholders,
                normalized_ids,
            ).fetchall()
        results: Dict[int, List[Dict[str, object]]] = {}
        seen = set()
        for row in rows:
            item = dict(row)
            key = (
                int(item["article_id"]),
                str(item["task_type"]),
                str(item["target_language"]),
                str(item["input_scope"]),
            )
            if key in seen:
                continue
            seen.add(key)
            results.setdefault(key[0], []).append(item)
        return results

    def latest_ai_digest(self, target_language: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_artifacts
                WHERE article_id IS NULL AND task_type='digest'
                  AND target_language=? AND status='succeeded'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (target_language,),
            ).fetchone()
        return dict(row) if row else None

    def ai_report_by_key(self, report_key: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT r.*,
                    aa.provider AS artifact_provider,
                    aa.requested_model AS artifact_requested_model,
                    aa.resolved_model AS artifact_resolved_model
                FROM ai_reports r
                JOIN ai_artifacts aa ON aa.id=r.artifact_id
                WHERE r.report_key=? AND aa.status='succeeded'
                """,
                (report_key,),
            ).fetchone()
        return dict(row) if row else None

    def latest_ai_reports(self) -> List[Dict[str, object]]:
        """Return the newest cached report for every period/language pair."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    r.id AS report_id,
                    r.report_key,
                    r.period,
                    r.timezone,
                    r.local_date,
                    r.period_start,
                    r.period_end,
                    r.target_language,
                    r.article_ids_json,
                    r.article_content_hash,
                    r.created_at,
                    aa.id AS artifact_id,
                    aa.input_scope,
                    aa.input_truncated,
                    aa.provider,
                    aa.requested_model,
                    aa.resolved_model,
                    aa.output_json,
                    aa.status
                FROM ai_reports r
                JOIN ai_artifacts aa ON aa.id=r.artifact_id
                WHERE aa.status='succeeded'
                ORDER BY r.created_at DESC, r.id DESC
                """
            ).fetchall()
        result: List[Dict[str, object]] = []
        seen = set()
        for row in rows:
            item = dict(row)
            key = (str(item["period"]), str(item["target_language"]))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def ensure_ai_job(
        self,
        *,
        artifact_key: str,
        article_id: Optional[int],
        task_type: str,
        input_scope: str,
        target_language: str,
        request: Dict[str, object],
        priority: int = 100,
        trigger_kind: str = "cli",
        max_attempts: int = 2,
        client_request_id: Optional[str] = None,
    ) -> Dict[str, object]:
        now = utc_now()
        request_json = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if client_request_id:
                existing_client = connection.execute(
                    "SELECT * FROM ai_jobs WHERE client_request_id=?",
                    (client_request_id,),
                ).fetchone()
                if existing_client is not None:
                    if (
                        str(existing_client["artifact_key"]) != artifact_key
                        or str(existing_client["request_json"]) != request_json
                    ):
                        connection.rollback()
                        raise AIJobConflict(
                            "client_request_id was already bound to a different AI request"
                        )
                    connection.commit()
                    return dict(existing_client)
            connection.execute(
                """
                INSERT OR IGNORE INTO ai_jobs(
                    artifact_key, article_id, task_type, input_scope, target_language,
                    request_json, priority, trigger_kind, state, max_attempts,
                    client_request_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    artifact_key,
                    int(article_id) if article_id is not None else None,
                    task_type,
                    input_scope,
                    target_language,
                    request_json,
                    int(priority),
                    trigger_kind,
                    int(max_attempts),
                    client_request_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ai_jobs WHERE artifact_key=?", (artifact_key,)
            ).fetchone()
            connection.commit()
        if row is None:
            raise sqlite3.IntegrityError("AI job was not created")
        return dict(row)

    def ai_job(self, job_id: int) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
        return dict(row) if row else None

    def ai_job_for_artifact(self, artifact_key: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_jobs WHERE artifact_key=?", (artifact_key,)
            ).fetchone()
        return dict(row) if row else None

    def lease_ai_job(
        self, worker_id: str, *, lease_seconds: int = 300
    ) -> Optional[Dict[str, object]]:
        now = utc_now()
        lease_until = (
            datetime.now(timezone.utc) + timedelta(seconds=max(30, int(lease_seconds)))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ai_jobs SET state='retryable', lease_owner=NULL, lease_until=NULL,
                    updated_at=?, last_error_code='lease_expired',
                    last_error_message='worker lease expired before a provider request was sent'
                WHERE state='leased' AND lease_until IS NOT NULL AND lease_until < ?
                """,
                (now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM ai_jobs
                WHERE state IN ('queued', 'retryable', 'budget_blocked')
                  AND attempt_count < max_attempts
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY priority ASC, created_at ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE ai_jobs SET state='leased', lease_owner=?, lease_until=?,
                    updated_at=?, last_error_code='', last_error_message=''
                WHERE id=? AND state IN ('queued', 'retryable', 'budget_blocked')
                """,
                (worker_id, lease_until, now, int(row["id"])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            leased = connection.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (int(row["id"]),)
            ).fetchone()
            connection.commit()
        return dict(leased) if leased else None

    @staticmethod
    def _validated_ai_usage_ledger_entry(
        raw: Mapping[str, object],
    ) -> Dict[str, object]:
        """Validate one public, aggregate-only carried-usage bucket.

        The ledger deliberately has no provider identifiers, prompts, errors,
        or response bodies.  Timestamps are canonical UTC strings so SQLite's
        lexical comparisons are chronological and deterministic.
        """

        expected = {
            "timezone",
            "day_start",
            "day_end",
            "covered_through",
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
        }
        if set(raw) != expected:
            raise ValueError("AI usage ledger entry has unexpected fields")

        timezone_name = raw.get("timezone")
        if (
            not isinstance(timezone_name, str)
            or not timezone_name
            or len(timezone_name) > 64
            or any(ord(character) < 32 for character in timezone_name)
        ):
            raise ValueError("AI usage ledger timezone is invalid")

        def timestamp(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.endswith("Z"):
                raise ValueError("AI usage ledger %s must be canonical UTC" % name)
            try:
                parsed = datetime.fromisoformat(value[:-1] + "+00:00")
            except ValueError as exc:
                raise ValueError(
                    "AI usage ledger %s must be canonical UTC" % name
                ) from exc
            canonical = (
                parsed.astimezone(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            if canonical != value:
                raise ValueError("AI usage ledger %s must be canonical UTC" % name)
            return value

        day_start = timestamp("day_start")
        day_end = timestamp("day_end")
        covered_through = timestamp("covered_through")
        if day_start >= day_end or covered_through < day_start:
            raise ValueError("AI usage ledger window is invalid")

        result: Dict[str, object] = {
            "timezone": timezone_name,
            "day_start": day_start,
            "day_end": day_end,
            "covered_through": covered_through,
        }
        for name in expected - set(result):
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("AI usage ledger %s must be an integer" % name)
            # Keep aggregate SUM operations comfortably inside signed 64-bit
            # SQLite integers even at the maximum accepted row count.
            if value < 0 or value > 1_000_000_000_000:
                raise ValueError("AI usage ledger %s is outside the safe range" % name)
            result[name] = int(value)
        if int(result["confirmed_requests"]) + int(
            result["unconfirmed_requests"]
        ) != int(result["requests"]):
            raise ValueError("AI usage ledger request counts are inconsistent")
        return result

    @classmethod
    def _replace_ai_usage_ledger(
        cls,
        connection: sqlite3.Connection,
        entries: Sequence[Mapping[str, object]],
    ) -> None:
        """Replace imported carry rows inside the caller's transaction."""

        if len(entries) > 20_000:
            raise ValueError("AI usage ledger has too many entries")
        validated = [cls._validated_ai_usage_ledger_entry(entry) for entry in entries]
        validated.sort(key=lambda entry: str(entry["day_start"]))
        previous_end = ""
        timezone_name = str(validated[0]["timezone"]) if validated else ""
        for entry in validated:
            if str(entry["timezone"]) != timezone_name:
                raise ValueError("AI usage ledger timezones must match")
            if previous_end and str(entry["day_start"]) < previous_end:
                raise ValueError("AI usage ledger windows overlap")
            previous_end = str(entry["day_end"])

        connection.execute("DELETE FROM ai_usage_ledger")
        connection.executemany(
            """
            INSERT INTO ai_usage_ledger(
                timezone, day_start, day_end, covered_through, requests,
                confirmed_requests, unconfirmed_requests, input_tokens,
                cached_input_tokens, cache_miss_input_tokens,
                cache_write_input_tokens, output_tokens, reasoning_tokens,
                total_tokens, reserved_total_tokens_for_unconfirmed,
                cost_micros, reserved_cost_micros_for_unconfirmed
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                tuple(
                    entry[name]
                    for name in (
                        "timezone",
                        "day_start",
                        "day_end",
                        "covered_through",
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
                    )
                )
                for entry in validated
            ],
        )

    def replace_ai_usage_ledger(
        self, entries: Sequence[Mapping[str, object]]
    ) -> None:
        """Atomically replace the aggregate carry-forward usage ledger."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_ai_usage_ledger(connection, entries)
            connection.commit()

    def list_ai_usage_ledger(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_usage_ledger ORDER BY day_start ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_ai_usage_attempt_rows(self) -> List[Dict[str, object]]:
        """Return unbounded, identifier-free rows needed for public rollups."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT request_started_at, reservation_active,
                    reserved_total_tokens, reserved_cost_micros,
                    actual_input_tokens, actual_cached_input_tokens,
                    actual_cache_write_tokens, actual_output_tokens,
                    actual_reasoning_tokens, actual_total_tokens,
                    actual_cost_micros
                FROM ai_attempts
                ORDER BY request_started_at ASC, id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _canonical_hold_descriptor(value: object) -> Tuple[Dict[str, object], str]:
        if not isinstance(value, Mapping):
            raise ValueError("AI generation hold descriptor must be an object")

        forbidden = {
            "api_key",
            "authorization",
            "provider_request_id",
            "request_id",
            "response_id",
            "error",
            "error_code",
            "error_message",
            "messages",
            "instructions",
            "output",
            "response_body",
            "request_body",
        }

        def inspect(item: object) -> None:
            if isinstance(item, Mapping):
                for key, nested in item.items():
                    if not isinstance(key, str) or key in forbidden:
                        raise ValueError(
                            "AI generation hold descriptor contains a forbidden field"
                        )
                    inspect(nested)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested)
            elif item is not None and not isinstance(item, (str, int, float, bool)):
                raise ValueError("AI generation hold descriptor contains an invalid value")

        descriptor = dict(value)
        inspect(descriptor)
        try:
            encoded = json.dumps(
                descriptor,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("AI generation hold descriptor is not strict JSON") from exc
        if not encoded or len(encoded.encode("utf-8")) > 100_000:
            raise ValueError("AI generation hold descriptor is outside the safe size")
        decoded = json.loads(encoded)
        if not isinstance(decoded, dict):
            raise ValueError("AI generation hold descriptor must be an object")
        return decoded, encoded

    @staticmethod
    def _canonical_hold_timestamp(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("AI generation hold %s must be canonical UTC" % field)
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError(
                "AI generation hold %s must be canonical UTC" % field
            ) from exc
        canonical = (
            parsed.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        if canonical != value:
            raise ValueError("AI generation hold %s must be canonical UTC" % field)
        return value

    @classmethod
    def _validated_ai_generation_hold(
        cls, raw: Mapping[str, object]
    ) -> Dict[str, object]:
        expected = {
            "hold_key",
            "workload_kind",
            "hold_class",
            "descriptor",
            "first_seen_at",
            "last_seen_at",
        }
        if set(raw) != expected:
            raise ValueError("AI generation hold has unexpected fields")
        descriptor, descriptor_json = cls._canonical_hold_descriptor(
            raw.get("descriptor")
        )
        workload_kind = raw.get("workload_kind")
        if workload_kind not in {"article", "article_pair", "digest", "report"}:
            raise ValueError("AI generation hold workload_kind is invalid")
        if descriptor.get("workload_kind") != workload_kind:
            raise ValueError("AI generation hold descriptor workload differs")
        hold_class = raw.get("hold_class")
        if hold_class not in {"ambiguous", "paid_failure", "fallback_pending"}:
            raise ValueError("AI generation hold class is invalid")
        hold_key = raw.get("hold_key")
        expected_key = hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest()
        if hold_key != expected_key:
            raise ValueError("AI generation hold key does not match descriptor")
        first_seen = cls._canonical_hold_timestamp(
            raw.get("first_seen_at"), "first_seen_at"
        )
        last_seen = cls._canonical_hold_timestamp(
            raw.get("last_seen_at"), "last_seen_at"
        )
        if first_seen > last_seen:
            raise ValueError("AI generation hold timestamps are inconsistent")
        return {
            "hold_key": expected_key,
            "workload_kind": workload_kind,
            "hold_class": hold_class,
            "descriptor": descriptor,
            "descriptor_json": descriptor_json,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
        }

    @classmethod
    def _replace_ai_generation_holds(
        cls,
        connection: sqlite3.Connection,
        entries: Sequence[Mapping[str, object]],
    ) -> None:
        if len(entries) > 20_000:
            raise ValueError("AI generation hold list has too many entries")
        validated = [cls._validated_ai_generation_hold(entry) for entry in entries]
        if len({str(entry["hold_key"]) for entry in validated}) != len(validated):
            raise ValueError("AI generation hold keys must be unique")
        revisions = cls._reserve_ai_generation_hold_revisions(
            connection,
            len(validated),
        )
        connection.execute("DELETE FROM ai_generation_holds")
        connection.executemany(
            """
            INSERT INTO ai_generation_holds(
                hold_key, workload_kind, hold_class, descriptor_json,
                first_seen_at, last_seen_at, revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    entry["hold_key"],
                    entry["workload_kind"],
                    entry["hold_class"],
                    entry["descriptor_json"],
                    entry["first_seen_at"],
                    entry["last_seen_at"],
                    revision,
                )
                for entry, revision in zip(validated, revisions)
            ],
        )

    @staticmethod
    def _ensure_ai_generation_hold_revision_sequence(
        connection: sqlite3.Connection,
    ) -> int:
        maximum = int(
            connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM ai_generation_holds"
            ).fetchone()[0]
        )
        row = connection.execute(
            "SELECT value FROM app_meta "
            "WHERE key='ai_generation_hold_revision'"
        ).fetchone()
        stored = 0
        if row is not None:
            try:
                stored = int(row[0])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "invalid ai_generation_hold_revision"
                ) from exc
            if stored < 0:
                raise ValueError("invalid ai_generation_hold_revision")
        current = max(maximum, stored)
        connection.execute(
            "INSERT INTO app_meta(key, value) "
            "VALUES('ai_generation_hold_revision', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(current),),
        )
        return current

    @classmethod
    def _reserve_ai_generation_hold_revisions(
        cls,
        connection: sqlite3.Connection,
        count: int,
    ) -> Tuple[int, ...]:
        if count < 0:
            raise ValueError("AI generation hold revision count is invalid")
        if count == 0:
            return ()
        current = cls._ensure_ai_generation_hold_revision_sequence(connection)
        final = current + int(count)
        connection.execute(
            "UPDATE app_meta SET value=? "
            "WHERE key='ai_generation_hold_revision'",
            (str(final),),
        )
        return tuple(range(current + 1, final + 1))

    @classmethod
    def _upsert_ai_generation_hold(
        cls,
        connection: sqlite3.Connection,
        hold: Mapping[str, object],
        *,
        now: str,
        allow_risk_downgrade: bool = False,
    ) -> None:
        template_expected = {"hold_key", "workload_kind", "hold_class", "descriptor"}
        if set(hold) != template_expected:
            raise ValueError("AI generation hold template has unexpected fields")
        validated = cls._validated_ai_generation_hold(
            {
                **dict(hold),
                "first_seen_at": now,
                "last_seen_at": now,
            }
        )
        existing = connection.execute(
            "SELECT descriptor_json, first_seen_at FROM ai_generation_holds WHERE hold_key=?",
            (validated["hold_key"],),
        ).fetchone()
        if existing is not None and str(existing["descriptor_json"]) != str(
            validated["descriptor_json"]
        ):
            raise sqlite3.IntegrityError("AI generation hold hash collision")
        first_seen = (
            str(existing["first_seen_at"])
            if existing is not None
            else str(validated["first_seen_at"])
        )
        hold_class = str(validated["hold_class"])
        if existing is not None:
            old_class = connection.execute(
                "SELECT hold_class FROM ai_generation_holds WHERE hold_key=?",
                (validated["hold_key"],),
            ).fetchone()
            if old_class is not None:
                risk = {
                    "fallback_pending": 0,
                    "paid_failure": 1,
                    "ambiguous": 2,
                }
                previous = str(old_class["hold_class"])
                if (
                    not allow_risk_downgrade
                    and risk.get(previous, 2) > risk.get(hold_class, 2)
                ):
                    hold_class = previous
        revision = cls._reserve_ai_generation_hold_revisions(
            connection,
            1,
        )[0]
        connection.execute(
            """
            INSERT INTO ai_generation_holds(
                hold_key, workload_kind, hold_class, descriptor_json,
                first_seen_at, last_seen_at, revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hold_key) DO UPDATE SET
                hold_class=excluded.hold_class,
                last_seen_at=excluded.last_seen_at,
                revision=excluded.revision
            """,
            (
                validated["hold_key"],
                validated["workload_kind"],
                hold_class,
                validated["descriptor_json"],
                first_seen,
                now,
                revision,
            ),
        )

    def replace_ai_generation_holds(
        self, entries: Sequence[Mapping[str, object]]
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._replace_ai_generation_holds(connection, entries)
            connection.commit()

    def list_ai_generation_holds(
        self,
        *,
        include_revision: bool = False,
    ) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_generation_holds ORDER BY hold_key ASC"
            ).fetchall()
        result = [
            {
                "hold_key": str(row["hold_key"]),
                "workload_kind": str(row["workload_kind"]),
                "hold_class": str(row["hold_class"]),
                "descriptor": json.loads(str(row["descriptor_json"])),
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
            }
            for row in rows
        ]
        if include_revision:
            for item, row in zip(result, rows):
                item["revision"] = int(row["revision"])
        return result

    def ai_generation_hold(
        self,
        hold_key: str,
        *,
        include_revision: bool = False,
    ) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_generation_holds WHERE hold_key=?",
                (str(hold_key),),
            ).fetchone()
        if row is None:
            return None
        result = {
            "hold_key": str(row["hold_key"]),
            "workload_kind": str(row["workload_kind"]),
            "hold_class": str(row["hold_class"]),
            "descriptor": json.loads(str(row["descriptor_json"])),
            "first_seen_at": str(row["first_seen_at"]),
            "last_seen_at": str(row["last_seen_at"]),
        }
        if include_revision:
            result["revision"] = int(row["revision"])
        return result

    def clear_ai_generation_hold(self, hold_key: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM ai_generation_holds WHERE hold_key=?",
                (str(hold_key),),
            )
        return cursor.rowcount == 1

    def clear_ai_generation_hold_if_class(
        self,
        hold_key: str,
        hold_class: str,
    ) -> bool:
        """Delete a hold only if its risk class still matches a prior read."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM ai_generation_holds
                WHERE hold_key=? AND hold_class=?
                """,
                (str(hold_key), str(hold_class)),
            )
        return cursor.rowcount == 1

    def clear_ai_generation_hold_if_snapshot(
        self,
        hold_key: str,
        hold_class: str,
        revision: int,
    ) -> bool:
        """Delete a hold only when its preflight snapshot is unchanged."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM ai_generation_holds
                WHERE hold_key=? AND hold_class=? AND revision=?
                """,
                (
                    str(hold_key),
                    str(hold_class),
                    int(revision),
                ),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _ai_usage_totals(
        connection: sqlite3.Connection, started_at: str
    ) -> Dict[str, int]:
        local = connection.execute(
            """
            SELECT COUNT(*) AS requests,
                COALESCE(SUM(
                    CASE
                        WHEN reservation_active=1 THEN reserved_total_tokens
                        WHEN actual_total_tokens IS NOT NULL THEN actual_total_tokens
                        ELSE 0
                    END
                ), 0) AS total_tokens,
                COALESCE(SUM(
                    CASE
                        WHEN reservation_active=1 THEN reserved_cost_micros
                        WHEN actual_cost_micros IS NOT NULL THEN actual_cost_micros
                        ELSE 0
                    END
                ), 0) AS cost_micros
            FROM ai_attempts AS attempt
            WHERE request_started_at >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM ai_usage_ledger AS carried
                  WHERE attempt.request_started_at >= carried.day_start
                    AND attempt.request_started_at < carried.day_end
                    AND attempt.request_started_at <= carried.covered_through
              )
            """,
            (started_at,),
        ).fetchone()
        carried = connection.execute(
            """
            SELECT COALESCE(SUM(requests), 0) AS requests,
                COALESCE(SUM(
                    total_tokens + reserved_total_tokens_for_unconfirmed
                ), 0) AS total_tokens,
                COALESCE(SUM(
                    cost_micros + reserved_cost_micros_for_unconfirmed
                ), 0) AS cost_micros
            FROM ai_usage_ledger
            WHERE day_end > ?
            """,
            (started_at,),
        ).fetchone()
        return {
            "requests": int(local["requests"] or 0)
            + int(carried["requests"] or 0),
            "total_tokens": int(local["total_tokens"] or 0)
            + int(carried["total_tokens"] or 0),
            "cost_micros": int(local["cost_micros"] or 0)
            + int(carried["cost_micros"] or 0),
        }

    def reserve_ai_attempt(
        self,
        *,
        job_id: int,
        idempotency_key: str,
        requested_model: str,
        estimated_input_tokens: int,
        reserved_output_tokens: int,
        reserved_cost_micros: int,
        price_snapshot: Dict[str, object],
        daily_started_at: str,
        monthly_started_at: str,
        daily_reset_at: str,
        monthly_reset_at: str,
        daily_max_requests: int,
        daily_max_total_tokens: int,
        daily_max_cost_micros: int,
        monthly_max_requests: int,
        monthly_max_total_tokens: int,
        monthly_max_cost_micros: int,
        requested_provider: str = "unknown",
    ) -> Dict[str, object]:
        now = utc_now()
        estimated_input = max(1, int(estimated_input_tokens))
        reserved_output = max(1, int(reserved_output_tokens))
        reserved_total = estimated_input + reserved_output
        reserved_cost = max(0, int(reserved_cost_micros))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if job is None:
                connection.rollback()
                raise KeyError("unknown AI job: %s" % job_id)
            cached = connection.execute(
                "SELECT * FROM ai_artifacts WHERE artifact_key=?",
                (job["artifact_key"],),
            ).fetchone()
            if cached is not None:
                connection.execute(
                    """
                    UPDATE ai_jobs SET state='succeeded', artifact_id=?, lease_owner=NULL,
                        lease_until=NULL, updated_at=?, last_error_code='',
                        last_error_message=''
                    WHERE id=?
                    """,
                    (int(cached["id"]), now, int(job_id)),
                )
                connection.commit()
                result = dict(cached)
                result["cache_hit"] = True
                return result
            if str(job["state"]) not in {
                "queued",
                "leased",
                "retryable",
                "budget_blocked",
            }:
                connection.rollback()
                raise AIJobConflict(
                    "AI job %s is already in state %s" % (job_id, job["state"])
                )
            if int(job["attempt_count"]) >= int(job["max_attempts"]):
                connection.execute(
                    """
                    UPDATE ai_jobs SET state='permanent_failed', updated_at=?,
                        last_error_code='attempt_limit',
                        last_error_message='maximum provider attempts reached'
                    WHERE id=?
                    """,
                    (now, int(job_id)),
                )
                connection.commit()
                raise AIJobConflict("maximum provider attempts reached")

            daily = self._ai_usage_totals(connection, daily_started_at)
            monthly = self._ai_usage_totals(connection, monthly_started_at)
            impossible: List[str] = []
            blocked: List[Tuple[str, str]] = []
            if daily_max_requests <= 0:
                impossible.append("daily request budget is zero")
            elif daily["requests"] + 1 > daily_max_requests:
                blocked.append(("daily request budget exhausted", daily_reset_at))
            if daily_max_total_tokens <= 0 or reserved_total > daily_max_total_tokens:
                impossible.append("request exceeds the daily token budget")
            elif daily["total_tokens"] + reserved_total > daily_max_total_tokens:
                blocked.append(("daily token budget exhausted", daily_reset_at))
            if monthly_max_requests <= 0:
                impossible.append("monthly request budget is zero")
            elif monthly["requests"] + 1 > monthly_max_requests:
                blocked.append(("monthly request budget exhausted", monthly_reset_at))
            if monthly_max_total_tokens <= 0 or reserved_total > monthly_max_total_tokens:
                impossible.append("request exceeds the monthly token budget")
            elif monthly["total_tokens"] + reserved_total > monthly_max_total_tokens:
                blocked.append(("monthly token budget exhausted", monthly_reset_at))
            if daily_max_cost_micros > 0:
                if reserved_cost > daily_max_cost_micros:
                    impossible.append("request exceeds the daily cost budget")
                elif daily["cost_micros"] + reserved_cost > daily_max_cost_micros:
                    blocked.append(("daily cost budget exhausted", daily_reset_at))
            if monthly_max_cost_micros > 0:
                if reserved_cost > monthly_max_cost_micros:
                    impossible.append("request exceeds the monthly cost budget")
                elif monthly["cost_micros"] + reserved_cost > monthly_max_cost_micros:
                    blocked.append(("monthly cost budget exhausted", monthly_reset_at))
            if impossible or blocked:
                reason = impossible[0] if impossible else blocked[0][0]
                state = "permanent_failed" if impossible else "budget_blocked"
                error_code = "budget_impossible" if impossible else "budget_exhausted"
                next_attempt_at = None if impossible else max(reset for _, reset in blocked)
                connection.execute(
                    """
                    UPDATE ai_jobs SET state=?, lease_owner=NULL,
                        lease_until=NULL, next_attempt_at=?, updated_at=?,
                        last_error_code=?, last_error_message=? WHERE id=?
                    """,
                    (
                        state,
                        next_attempt_at,
                        now,
                        error_code,
                        reason,
                        int(job_id),
                    ),
                )
                connection.commit()
                raise AIBudgetExceeded(reason)

            attempt_number = int(job["attempt_count"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO ai_attempts(
                    job_id, attempt_number, idempotency_key, state,
                    request_started_at, requested_provider, requested_model,
                    estimated_input_tokens,
                    reserved_output_tokens, reserved_total_tokens,
                    reserved_cost_micros, price_snapshot_json
                ) VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(job_id),
                    attempt_number,
                    idempotency_key,
                    now,
                    str(requested_provider or "unknown")[:100],
                    requested_model,
                    estimated_input,
                    reserved_output,
                    reserved_total,
                    reserved_cost,
                    json.dumps(
                        price_snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.execute(
                """
                UPDATE ai_jobs SET state='reserved', attempt_count=?, updated_at=?,
                    lease_owner=NULL, lease_until=NULL, last_error_code='',
                    last_error_message='' WHERE id=?
                """,
                (attempt_number, now, int(job_id)),
            )
            attempt = connection.execute(
                "SELECT * FROM ai_attempts WHERE id=?", (int(cursor.lastrowid),)
            ).fetchone()
            connection.commit()
        result = dict(attempt) if attempt else {}
        result["cache_hit"] = False
        return result

    def mark_ai_attempt_sent(
        self,
        attempt_id: int,
        *,
        provisional_generation_hold: Mapping[str, object],
    ) -> bool:
        """Atomically mark a request sent and install its no-replay hold.

        The return value is true only when this call created the exact hold.
        A caller may settle that newly created provisional ``ambiguous`` hold
        to a lower-risk definitive outcome.  A pre-existing hold must retain
        its risk class, including when an operator explicitly forced replay.
        """

        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id, state FROM ai_attempts WHERE id=?", (int(attempt_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("unknown AI attempt: %s" % attempt_id)
            if row["state"] != "reserved":
                connection.rollback()
                raise AIJobConflict("AI attempt is already in state %s" % row["state"])
            hold_key = str(provisional_generation_hold.get("hold_key") or "")
            existing_hold = connection.execute(
                "SELECT hold_class FROM ai_generation_holds WHERE hold_key=?",
                (hold_key,),
            ).fetchone()
            self._upsert_ai_generation_hold(
                connection,
                provisional_generation_hold,
                now=now,
            )
            provisional_created = existing_hold is None
            connection.execute(
                "UPDATE ai_attempts SET state='sent' WHERE id=?", (int(attempt_id),)
            )
            connection.execute(
                "UPDATE ai_jobs SET state='sent', updated_at=? WHERE id=?",
                (now, int(row["job_id"])),
            )
            connection.commit()
        return provisional_created

    def complete_ai_attempt(
        self,
        *,
        attempt_id: int,
        artifact: Dict[str, object],
        additional_artifacts: Sequence[Dict[str, object]] = (),
        report_records: Sequence[Mapping[str, object]] = (),
        report_article_versions: Optional[Mapping[int, str]] = None,
        usage: Dict[str, int],
        actual_cost_micros: Optional[int] = None,
        usage_confirmed: bool = True,
        clear_generation_hold_key: str = "",
        clear_generation_hold_snapshots: Sequence[
            Tuple[str, str, int]
        ] = (),
    ) -> Dict[str, object]:
        now = utc_now()
        columns = (
            "article_id", "task_type", "input_scope", "source_language",
            "target_language", "artifact_key", "input_hash", "article_content_hash",
            "source_artifact_id", "content_snapshot_id", "prompt_version", "prompt_hash",
            "response_schema_version", "response_schema_hash", "provider",
            "requested_model", "resolved_model", "generation_params_hash",
            "provider_response_id", "output_json", "output_text", "output_hash",
            "status", "input_truncated", "created_at",
        )
        artifacts = [artifact] + list(additional_artifacts)
        if report_records and len(report_records) != len(artifacts):
            raise AIJobConflict(
                "AI attempt report records must match committed artifacts"
            )
        if report_records and report_article_versions is None:
            raise AIJobConflict(
                "AI attempt report records require article versions"
            )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT * FROM ai_attempts WHERE id=?", (int(attempt_id),)
            ).fetchone()
            if attempt is None:
                connection.rollback()
                raise KeyError("unknown AI attempt: %s" % attempt_id)
            if attempt["state"] != "sent":
                connection.rollback()
                raise AIJobConflict("AI attempt is already in state %s" % attempt["state"])
            stored_rows = []
            seen_keys = set()
            for candidate in artifacts:
                artifact_key = str(candidate.get("artifact_key") or "")
                if not artifact_key or artifact_key in seen_keys:
                    connection.rollback()
                    raise AIJobConflict(
                        "AI attempt artifacts require distinct cache keys"
                    )
                seen_keys.add(artifact_key)
                values = [candidate.get(column) for column in columns[:-1]] + [now]
                connection.execute(
                    "INSERT OR IGNORE INTO ai_artifacts(%s) VALUES (%s)"
                    % (", ".join(columns), ", ".join("?" for _ in columns)),
                    values,
                )
                stored_candidate = connection.execute(
                    "SELECT * FROM ai_artifacts WHERE artifact_key=?",
                    (artifact_key,),
                ).fetchone()
                if stored_candidate is None:
                    connection.rollback()
                    raise sqlite3.IntegrityError("AI artifact was not stored")
                stored_rows.append(stored_candidate)
            if report_records:
                normalized, first_report, expected_versions = (
                    self._normalize_ai_report_attachments(
                        [
                            (dict(stored_row), dict(report))
                            for stored_row, report in zip(
                                stored_rows, report_records
                            )
                        ],
                        report_article_versions or {},
                    )
                )
                self._attach_existing_ai_reports_in_connection(
                    connection,
                    normalized,
                    first_report=first_report,
                    expected_versions=expected_versions,
                    now=now,
                )
            stored = stored_rows[0]
            recorded_usage = usage if usage_confirmed else {}
            actual_total = recorded_usage.get("total_tokens")
            connection.execute(
                """
                UPDATE ai_attempts SET state='succeeded', response_received_at=?,
                    provider_request_id=?, resolved_model=?, actual_input_tokens=?,
                    actual_cached_input_tokens=?, actual_cache_write_tokens=?,
                    actual_output_tokens=?, actual_reasoning_tokens=?, actual_total_tokens=?,
                    actual_cost_micros=?, reservation_active=?, http_status=?,
                    finish_reason=?, response_hash=?
                WHERE id=?
                """,
                (
                    now,
                    artifact.get("provider_response_id", ""),
                    artifact.get("resolved_model", ""),
                    recorded_usage.get("input_tokens"),
                    recorded_usage.get("cached_input_tokens"),
                    recorded_usage.get("cache_write_tokens"),
                    recorded_usage.get("output_tokens"),
                    recorded_usage.get("reasoning_tokens"),
                    actual_total,
                    actual_cost_micros if usage_confirmed else None,
                    0 if usage_confirmed else 1,
                    int(artifact.get("http_status") or 200),
                    str(artifact.get("finish_reason") or "completed")[:100],
                    str(artifact.get("response_hash") or artifact.get("output_hash") or ""),
                    int(attempt_id),
                ),
            )
            connection.execute(
                """
                UPDATE ai_jobs SET state='succeeded', artifact_id=?, updated_at=?,
                    lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL,
                    last_error_code='', last_error_message=''
                WHERE id=?
                """,
                (int(stored["id"]), now, int(attempt["job_id"])),
            )
            if clear_generation_hold_key:
                connection.execute(
                    "DELETE FROM ai_generation_holds WHERE hold_key=?",
                    (str(clear_generation_hold_key),),
                )
            for hold_key, hold_class, revision in dict.fromkeys(
                clear_generation_hold_snapshots
            ):
                connection.execute(
                    """
                    DELETE FROM ai_generation_holds
                    WHERE hold_key=? AND hold_class=? AND revision=?
                    """,
                    (
                        str(hold_key),
                        str(hold_class),
                        int(revision),
                    ),
                )
            connection.commit()
        return dict(stored)

    def store_external_ai_artifacts(
        self, artifacts: Sequence[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        """Atomically cache validated artifacts produced outside the API sidecar.

        This path deliberately creates no ``ai_attempts`` row: the fixed reader
        program did not make a provider request and therefore has no billable
        API usage to reserve or audit.  Existing artifact keys are immutable
        cache hits, making a repeated import idempotent.
        """

        if not artifacts:
            return []
        now = utc_now()
        columns = (
            "article_id", "task_type", "input_scope", "source_language",
            "target_language", "artifact_key", "input_hash", "article_content_hash",
            "source_artifact_id", "content_snapshot_id", "prompt_version", "prompt_hash",
            "response_schema_version", "response_schema_hash", "provider",
            "requested_model", "resolved_model", "generation_params_hash",
            "provider_response_id", "output_json", "output_text", "output_hash",
            "status", "input_truncated", "created_at",
        )
        stored_artifacts: List[Dict[str, object]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for artifact in artifacts:
                article_id = artifact.get("article_id")
                if article_id is None:
                    connection.rollback()
                    raise AIJobConflict(
                        "external AI imports support article artifacts only"
                    )
                article = connection.execute(
                    "SELECT content_hash FROM articles WHERE id=?",
                    (int(article_id),),
                ).fetchone()
                if article is None:
                    connection.rollback()
                    raise AIJobConflict(
                        "article %s disappeared before AI import" % article_id
                    )
                if str(article["content_hash"] or "") != str(
                    artifact.get("article_content_hash") or ""
                ):
                    connection.rollback()
                    raise AIJobConflict(
                        "article %s changed before AI import" % article_id
                    )
                values = [artifact.get(column) for column in columns[:-1]] + [now]
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO ai_artifacts(%s) VALUES (%s)"
                    % (", ".join(columns), ", ".join("?" for _ in columns)),
                    values,
                )
                stored = connection.execute(
                    "SELECT * FROM ai_artifacts WHERE artifact_key=?",
                    (artifact["artifact_key"],),
                ).fetchone()
                if stored is None:
                    connection.rollback()
                    raise sqlite3.IntegrityError("AI artifact was not stored")
                result = dict(stored)
                result["cache_hit"] = cursor.rowcount == 0
                stored_artifacts.append(result)
            connection.commit()
        return stored_artifacts

    def store_external_ai_report(
        self,
        artifact: Dict[str, object],
        report: Dict[str, object],
        *,
        article_versions: Mapping[int, str],
    ) -> Dict[str, object]:
        """Atomically cache a subscription-produced digest and report record.

        The transaction rechecks every covered article version immediately
        before insertion.  Like ``store_external_ai_artifacts``, this path
        deliberately creates no provider attempt or budget reservation.
        """

        if artifact.get("article_id") is not None:
            raise AIJobConflict("external AI reports cannot belong to one article")
        if artifact.get("task_type") != "digest" or artifact.get("input_scope") != "digest":
            raise AIJobConflict("external AI reports require a digest artifact")
        if report.get("period") not in {"daily", "weekly"}:
            raise AIJobConflict("external AI report period is invalid")
        expected_versions = {
            int(article_id): str(content_hash or "")
            for article_id, content_hash in article_versions.items()
        }
        expected_ids = list(expected_versions)
        if not 1 <= len(expected_ids) <= 50:
            raise AIJobConflict("AI report selected article count is invalid")
        try:
            supplied_ids = json.loads(str(report.get("article_ids_json") or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIJobConflict("AI report article IDs are invalid") from exc
        if supplied_ids != expected_ids:
            raise AIJobConflict("AI report article IDs differ")

        now = utc_now()
        artifact_columns = (
            "article_id", "task_type", "input_scope", "source_language",
            "target_language", "artifact_key", "input_hash", "article_content_hash",
            "source_artifact_id", "content_snapshot_id", "prompt_version", "prompt_hash",
            "response_schema_version", "response_schema_hash", "provider",
            "requested_model", "resolved_model", "generation_params_hash",
            "provider_response_id", "output_json", "output_text", "output_hash",
            "status", "input_truncated", "created_at",
        )
        report_columns = (
            "report_key", "period", "timezone", "local_date", "period_start",
            "period_end", "target_language", "article_ids_json",
            "article_content_hash", "artifact_id", "created_at",
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_rows = connection.execute(
                """
                SELECT id, content_hash
                FROM articles
                WHERE julianday(COALESCE(published_at, discovered_at))
                          >= julianday(?)
                  AND julianday(COALESCE(published_at, discovered_at))
                          <= julianday(?)
                ORDER BY COALESCE(published_at, discovered_at) DESC,
                    source_slug ASC, external_id ASC, canonical_url ASC
                LIMIT ?
                """,
                (
                    report.get("period_start"),
                    report.get("period_end"),
                    len(expected_versions),
                ),
            ).fetchall()
            current_versions = {
                int(row["id"]): str(row["content_hash"] or "")
                for row in current_rows
            }
            if (
                list(current_versions) != list(expected_versions)
                or current_versions != expected_versions
            ):
                connection.rollback()
                raise AIJobConflict(
                    "the AI report article set changed before import"
                )
            for article_id, content_hash in article_versions.items():
                row = connection.execute(
                    "SELECT content_hash FROM articles WHERE id=?", (int(article_id),)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise AIJobConflict(
                        "article %s disappeared before AI report import" % article_id
                    )
                if str(row["content_hash"] or "") != str(content_hash or ""):
                    connection.rollback()
                    raise AIJobConflict(
                        "article %s changed before AI report import" % article_id
                    )

            artifact_values = [artifact.get(column) for column in artifact_columns[:-1]] + [now]
            artifact_cursor = connection.execute(
                "INSERT OR IGNORE INTO ai_artifacts(%s) VALUES (%s)"
                % (
                    ", ".join(artifact_columns),
                    ", ".join("?" for _ in artifact_columns),
                ),
                artifact_values,
            )
            stored_artifact = connection.execute(
                "SELECT * FROM ai_artifacts WHERE artifact_key=?",
                (artifact["artifact_key"],),
            ).fetchone()
            if stored_artifact is None:
                connection.rollback()
                raise sqlite3.IntegrityError("AI report artifact was not stored")

            report_values = [report.get(column) for column in report_columns[:-2]] + [
                int(stored_artifact["id"]),
                now,
            ]
            report_cursor = connection.execute(
                "INSERT OR IGNORE INTO ai_reports(%s) VALUES (%s)"
                % (
                    ", ".join(report_columns),
                    ", ".join("?" for _ in report_columns),
                ),
                report_values,
            )
            stored_report = connection.execute(
                "SELECT * FROM ai_reports WHERE report_key=?",
                (report["report_key"],),
            ).fetchone()
            if stored_report is None:
                connection.rollback()
                raise sqlite3.IntegrityError("AI report record was not stored")
            expected = {
                column: report.get(column)
                for column in report_columns[:-2]
            }
            for column, expected_value in expected.items():
                if str(stored_report[column]) != str(expected_value):
                    connection.rollback()
                    raise AIJobConflict("AI report cache key collision")
            if int(stored_report["artifact_id"]) != int(stored_artifact["id"]):
                connection.rollback()
                raise AIJobConflict("AI report artifact cache key collision")
            connection.commit()

        result = dict(stored_report)
        result["artifact_id"] = int(stored_artifact["id"])
        result["artifact_cache_hit"] = artifact_cursor.rowcount == 0
        result["cache_hit"] = report_cursor.rowcount == 0
        return result

    @staticmethod
    def _normalize_ai_report_attachments(
        records: Sequence[
            Tuple[Mapping[str, object], Mapping[str, object]]
        ],
        article_versions: Mapping[int, str],
    ) -> Tuple[
        List[Tuple[Dict[str, object], Dict[str, object]]],
        Dict[str, object],
        Dict[int, str],
    ]:
        if not records or len(records) > 2:
            raise AIJobConflict("AI report attachment requires one or two records")
        normalized = [
            (dict(artifact), dict(report)) for artifact, report in records
        ]
        first_report = normalized[0][1]
        window = (
            first_report.get("period"),
            first_report.get("timezone"),
            first_report.get("local_date"),
            first_report.get("period_start"),
            first_report.get("period_end"),
        )
        languages = set()
        for artifact, report in normalized:
            if artifact.get("article_id") is not None:
                raise AIJobConflict("AI report artifact cannot belong to one article")
            if (
                artifact.get("task_type") != "digest"
                or artifact.get("input_scope") != "digest"
            ):
                raise AIJobConflict("AI report attachment requires digest artifacts")
            if report.get("period") not in {"daily", "weekly"}:
                raise AIJobConflict("AI report period is invalid")
            if (
                report.get("period"),
                report.get("timezone"),
                report.get("local_date"),
                report.get("period_start"),
                report.get("period_end"),
            ) != window:
                raise AIJobConflict("AI report attachment windows differ")
            language = str(report.get("target_language") or "")
            if not language or language != str(
                artifact.get("target_language") or ""
            ):
                raise AIJobConflict("AI report artifact language differs")
            if language in languages:
                raise AIJobConflict(
                    "AI report attachment languages must be distinct"
                )
            languages.add(language)
            if str(report.get("article_content_hash") or "") != str(
                artifact.get("article_content_hash") or ""
            ):
                raise AIJobConflict("AI report artifact content hash differs")

        expected_versions = {
            int(article_id): str(content_hash or "")
            for article_id, content_hash in article_versions.items()
        }
        expected_ids = list(expected_versions)
        if not 1 <= len(expected_ids) <= 50:
            raise AIJobConflict("AI report selected article count is invalid")
        for _, report in normalized:
            try:
                supplied_ids = json.loads(
                    str(report.get("article_ids_json") or "")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AIJobConflict("AI report article IDs are invalid") from exc
            if supplied_ids != expected_ids:
                raise AIJobConflict("AI report article IDs differ")
        return normalized, first_report, expected_versions

    @staticmethod
    def _attach_existing_ai_reports_in_connection(
        connection: sqlite3.Connection,
        normalized: Sequence[
            Tuple[Mapping[str, object], Mapping[str, object]]
        ],
        *,
        first_report: Mapping[str, object],
        expected_versions: Mapping[int, str],
        now: str,
    ) -> List[Dict[str, object]]:
        current_rows = connection.execute(
            """
            SELECT id, content_hash
            FROM articles
            WHERE julianday(COALESCE(published_at, discovered_at))
                      >= julianday(?)
              AND julianday(COALESCE(published_at, discovered_at))
                      <= julianday(?)
            ORDER BY COALESCE(published_at, discovered_at) DESC,
                source_slug ASC, external_id ASC, canonical_url ASC
            LIMIT ?
            """,
            (
                first_report.get("period_start"),
                first_report.get("period_end"),
                len(expected_versions),
            ),
        ).fetchall()
        current_versions = {
            int(row["id"]): str(row["content_hash"] or "")
            for row in current_rows
        }
        if (
            list(current_versions) != list(expected_versions)
            or current_versions != expected_versions
        ):
            raise AIJobConflict(
                "the AI report article set changed before attachment"
            )

        report_columns = (
            "report_key", "period", "timezone", "local_date", "period_start",
            "period_end", "target_language", "article_ids_json",
            "article_content_hash", "artifact_id", "created_at",
        )
        results: List[Dict[str, object]] = []
        for artifact, report in normalized:
            artifact_id = artifact.get("id")
            if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
                raise AIJobConflict("AI report artifact ID is invalid")
            stored_artifact = connection.execute(
                "SELECT * FROM ai_artifacts WHERE id=?",
                (int(artifact_id),),
            ).fetchone()
            if stored_artifact is None:
                raise AIJobConflict("AI report artifact disappeared")
            for field in (
                "artifact_key",
                "task_type",
                "input_scope",
                "target_language",
                "input_hash",
                "article_content_hash",
                "output_hash",
                "status",
            ):
                if str(stored_artifact[field]) != str(artifact.get(field)):
                    raise AIJobConflict(
                        "AI report artifact changed before attachment"
                    )

            values = [report.get(column) for column in report_columns[:-2]] + [
                int(artifact_id),
                now,
            ]
            cursor = connection.execute(
                "INSERT OR IGNORE INTO ai_reports(%s) VALUES (%s)"
                % (
                    ", ".join(report_columns),
                    ", ".join("?" for _ in report_columns),
                ),
                values,
            )
            stored_report = connection.execute(
                "SELECT * FROM ai_reports WHERE report_key=?",
                (report["report_key"],),
            ).fetchone()
            if stored_report is None:
                raise sqlite3.IntegrityError("AI report record was not stored")
            for column in report_columns[:-2]:
                if str(stored_report[column]) != str(report.get(column)):
                    raise AIJobConflict("AI report cache key collision")
            if int(stored_report["artifact_id"]) != int(artifact_id):
                raise AIJobConflict("AI report artifact cache key collision")
            result = dict(stored_report)
            result["cache_hit"] = cursor.rowcount == 0
            results.append(result)
        return results

    def store_existing_ai_reports(
        self,
        records: Sequence[
            Tuple[Mapping[str, object], Mapping[str, object]]
        ],
        *,
        article_versions: Mapping[int, str],
    ) -> List[Dict[str, object]]:
        """Idempotently attach report rows to already committed artifacts.

        Provider-backed report generation now supplies these records directly
        to ``complete_ai_attempt`` so artifacts, reports, usage, and attempt
        state commit together.  This method remains for cache hits and for
        validated artifacts imported through the external bridge.
        """

        normalized, first_report, expected_versions = (
            self._normalize_ai_report_attachments(records, article_versions)
        )
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            results = self._attach_existing_ai_reports_in_connection(
                connection,
                normalized,
                first_report=first_report,
                expected_versions=expected_versions,
                now=now,
            )
            connection.commit()
        return results

    def fail_ai_attempt(
        self,
        *,
        attempt_id: int,
        job_state: str,
        error_class: str,
        error_code: str,
        error_message: str,
        http_status: Optional[int] = None,
        usage: Optional[Dict[str, int]] = None,
        actual_cost_micros: Optional[int] = None,
        next_attempt_at: Optional[str] = None,
        preserve_reservation: bool = False,
        provider_request_id: str = "",
        resolved_model: str = "",
        finish_reason: str = "",
        generation_hold: Optional[Mapping[str, object]] = None,
        settle_provisional_generation_hold: bool = False,
        clear_provisional_generation_hold_key: str = "",
    ) -> None:
        if job_state not in {"retryable", "unknown", "permanent_failed", "cancelled"}:
            raise ValueError("invalid failed AI job state: %s" % job_state)
        now = utc_now()
        clean_message = str(error_message).replace("\x00", "")[:1000]
        actual_usage = usage or {}
        unknown_result = job_state == "unknown"
        keep_reservation = unknown_result or bool(preserve_reservation)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempt = connection.execute(
                "SELECT job_id, state FROM ai_attempts WHERE id=?", (int(attempt_id),)
            ).fetchone()
            if attempt is None:
                connection.rollback()
                raise KeyError("unknown AI attempt: %s" % attempt_id)
            connection.execute(
                """
                UPDATE ai_attempts SET state=?, response_received_at=?, http_status=?,
                    provider_request_id=?, resolved_model=?, finish_reason=?,
                    error_class=?, error_code=?, error_message=?,
                    actual_input_tokens=?, actual_cached_input_tokens=?,
                    actual_cache_write_tokens=?, actual_output_tokens=?,
                    actual_reasoning_tokens=?, actual_total_tokens=?,
                    actual_cost_micros=?, reservation_active=?
                WHERE id=?
                """,
                (
                    "unknown" if unknown_result else "failed",
                    None if unknown_result else now,
                    http_status,
                    str(provider_request_id)[:200],
                    str(resolved_model)[:200],
                    str(finish_reason)[:100],
                    str(error_class)[:100],
                    str(error_code)[:100],
                    clean_message,
                    actual_usage.get("input_tokens"),
                    actual_usage.get("cached_input_tokens"),
                    actual_usage.get("cache_write_tokens"),
                    actual_usage.get("output_tokens"),
                    actual_usage.get("reasoning_tokens"),
                    actual_usage.get("total_tokens"),
                    actual_cost_micros,
                    int(keep_reservation),
                    int(attempt_id),
                ),
            )
            connection.execute(
                """
                UPDATE ai_jobs SET state=?, updated_at=?, lease_owner=NULL,
                    lease_until=NULL, next_attempt_at=?, last_error_code=?,
                    last_error_message=? WHERE id=?
                """,
                (
                    job_state,
                    now,
                    next_attempt_at,
                    str(error_code)[:100],
                    clean_message,
                    int(attempt["job_id"]),
                ),
            )
            if generation_hold is not None:
                self._upsert_ai_generation_hold(
                    connection,
                    generation_hold,
                    now=now,
                    allow_risk_downgrade=bool(
                        settle_provisional_generation_hold
                    ),
                )
            if clear_provisional_generation_hold_key:
                connection.execute(
                    "DELETE FROM ai_generation_holds WHERE hold_key=?",
                    (str(clear_provisional_generation_hold_key),),
                )
            connection.commit()

    def cancel_ai_job(self, job_id: int, code: str, message: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_jobs SET state='cancelled', updated_at=?, lease_owner=NULL,
                    lease_until=NULL, last_error_code=?, last_error_message=?
                WHERE id=? AND state IN ('queued', 'leased', 'retryable', 'budget_blocked')
                """,
                (now, code[:100], message[:1000], int(job_id)),
            )
        if cursor.rowcount != 1:
            raise AIJobConflict("AI job cannot be cancelled in its current state")

    def requeue_ai_job(
        self, job_id: int, *, allow_unknown: bool = False
    ) -> Dict[str, object]:
        now = utc_now()
        allowed = {"cancelled", "permanent_failed", "retryable", "budget_blocked"}
        if allow_unknown:
            allowed.add("unknown")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError("unknown AI job: %s" % job_id)
            if str(row["state"]) not in allowed:
                connection.rollback()
                raise AIJobConflict(
                    "AI job %s cannot be retried from state %s"
                    % (job_id, row["state"])
                )
            connection.execute(
                """
                UPDATE ai_jobs SET state='queued', artifact_id=NULL,
                    max_attempts=MAX(max_attempts, attempt_count + 1),
                    lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL,
                    updated_at=?, last_error_code='', last_error_message=''
                WHERE id=?
                """,
                (now, int(job_id)),
            )
            updated = connection.execute(
                "SELECT * FROM ai_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            connection.commit()
        return dict(updated)

    def recover_stalled_ai_jobs(self, *, older_than_seconds: int = 900) -> Dict[str, int]:
        """Recover only states whose billing ambiguity is known.

        A stale ``reserved`` attempt was never marked sent and can be released.
        A stale ``sent`` attempt is always moved to ``unknown`` and keeps its
        reservation; it is never automatically retried.
        """

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(60, int(older_than_seconds)))
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        now = utc_now()
        released = 0
        unknown = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT at.id AS attempt_id, at.job_id, at.state AS attempt_state,
                    j.attempt_count, j.max_attempts
                FROM ai_attempts at JOIN ai_jobs j ON j.id=at.job_id
                WHERE at.state IN ('reserved', 'sent')
                  AND j.state IN ('reserved', 'sent')
                  AND at.request_started_at < ?
                ORDER BY at.id
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                if row["attempt_state"] == "reserved":
                    next_state = (
                        "retryable"
                        if int(row["attempt_count"]) < int(row["max_attempts"])
                        else "permanent_failed"
                    )
                    connection.execute(
                        """
                        UPDATE ai_attempts SET state='failed', response_received_at=?,
                            reservation_active=0, error_class='worker_recovery',
                            error_code='reserved_before_send',
                            error_message='process stopped before the provider request was marked sent'
                        WHERE id=?
                        """,
                        (now, int(row["attempt_id"])),
                    )
                    connection.execute(
                        """
                        UPDATE ai_jobs SET state=?, lease_owner=NULL, lease_until=NULL,
                            next_attempt_at=NULL, updated_at=?,
                            last_error_code='reserved_before_send',
                            last_error_message='previous process stopped before sending the provider request'
                        WHERE id=?
                        """,
                        (next_state, now, int(row["job_id"])),
                    )
                    released += 1
                else:
                    connection.execute(
                        """
                        UPDATE ai_attempts SET state='unknown',
                            error_class='worker_recovery', error_code='sent_result_unknown',
                            error_message='process stopped after the provider request was marked sent',
                            reservation_active=1
                        WHERE id=?
                        """,
                        (int(row["attempt_id"]),),
                    )
                    connection.execute(
                        """
                        UPDATE ai_jobs SET state='unknown', lease_owner=NULL,
                            lease_until=NULL, updated_at=?,
                            last_error_code='sent_result_unknown',
                            last_error_message='previous provider request may have been billed; explicit retry is required'
                        WHERE id=?
                        """,
                        (now, int(row["job_id"])),
                    )
                    unknown += 1
            connection.commit()
        return {"released_before_send": released, "marked_unknown": unknown}

    def ai_status(self, daily_started_at: str, monthly_started_at: str) -> Dict[str, object]:
        with self.connect() as connection:
            daily = self._ai_usage_totals(connection, daily_started_at)
            monthly = self._ai_usage_totals(connection, monthly_started_at)
            job_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM ai_jobs GROUP BY state"
            ).fetchall()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM ai_artifacts) AS artifacts,
                    (SELECT COUNT(*) FROM article_content_snapshots) AS content_snapshots,
                    (SELECT COUNT(*) FROM ai_attempts) AS attempts
                """
            ).fetchone()
        return {
            "daily": daily,
            "monthly": monthly,
            "jobs": {str(row["state"]): int(row["count"]) for row in job_rows},
            "artifacts": int(counts["artifacts"] or 0),
            "content_snapshots": int(counts["content_snapshots"] or 0),
            "attempts": int(counts["attempts"] or 0),
        }

    def list_ai_attempts(self, limit: int = 100) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT at.*, j.article_id, j.task_type, j.input_scope,
                    j.target_language, j.trigger_kind, j.state AS job_state
                FROM ai_attempts at JOIN ai_jobs j ON j.id=at.job_id
                ORDER BY at.request_started_at DESC, at.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_ai_data(self, before: str, *, include_snapshots: bool = True) -> Dict[str, int]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ai_jobs SET state='cancelled', artifact_id=NULL,
                    lease_owner=NULL, lease_until=NULL, next_attempt_at=NULL,
                    updated_at=?, last_error_code='artifact_purged',
                    last_error_message='cached AI output was explicitly purged'
                WHERE artifact_id IN (
                    SELECT id FROM ai_artifacts WHERE created_at < ?
                )
                """,
                (utc_now(), before),
            )
            artifact_cursor = connection.execute(
                "DELETE FROM ai_artifacts WHERE created_at < ?", (before,)
            )
            snapshot_count = 0
            if include_snapshots:
                snapshot_cursor = connection.execute(
                    "DELETE FROM article_content_snapshots WHERE retrieved_at < ?",
                    (before,),
                )
                snapshot_count = int(snapshot_cursor.rowcount)
            connection.commit()
        return {
            "artifacts": int(artifact_cursor.rowcount),
            "content_snapshots": snapshot_count,
        }

    def source_statuses(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                    COALESCE(a.article_count, 0) AS article_count,
                    COALESCE(a.unread_count, 0) AS unread_count,
                    COALESCE(p.pending_count, 0) AS pending_count,
                    p.oldest_pending_at,
                    COALESCE(p.pending_errors, '') AS pending_errors,
                    COALESCE(c.auxiliary_errors, '') AS auxiliary_errors
                FROM sources s
                LEFT JOIN (
                    SELECT source_slug,
                        COUNT(*) AS article_count,
                        SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread_count
                    FROM articles GROUP BY source_slug
                ) a ON a.source_slug=s.slug
                LEFT JOIN (
                    SELECT source_slug,
                        COUNT(*) AS pending_count,
                        MIN(first_seen_at) AS oldest_pending_at,
                        GROUP_CONCAT(CASE WHEN last_error != '' THEN last_error END, '; ') AS pending_errors
                    FROM pending_urls GROUP BY source_slug
                ) p ON p.source_slug=s.slug
                LEFT JOIN (
                    SELECT source_slug,
                        GROUP_CONCAT(
                            CASE WHEN last_error != '' THEN check_name || ': ' || last_error END,
                            '; '
                        ) AS auxiliary_errors
                    FROM source_checks GROUP BY source_slug
                ) c ON c.source_slug=s.slug
                ORDER BY s.name
                """
            ).fetchall()
        statuses = [dict(row) for row in rows]
        now = datetime.now(timezone.utc)
        for status in statuses:
            if status.get("last_error"):
                health = "error"
            elif not status.get("last_success_at"):
                health = "never_synced"
            elif (
                status.get("auxiliary_errors")
                or status.get("pending_errors")
                or int(status.get("pending_count") or 0) > 0
            ):
                health = "degraded"
            else:
                try:
                    last_success = datetime.fromisoformat(
                        str(status["last_success_at"]).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    health = "stale" if (now - last_success).total_seconds() > 26 * 3600 else "healthy"
                except (TypeError, ValueError):
                    health = "stale"
            status["health"] = health
        return statuses

    def counts(self) -> Dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread,
                    SUM(CASE WHEN starred_at IS NOT NULL THEN 1 ELSE 0 END) AS starred
                FROM articles
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "unread": int(row["unread"] or 0),
            "starred": int(row["starred"] or 0),
        }
