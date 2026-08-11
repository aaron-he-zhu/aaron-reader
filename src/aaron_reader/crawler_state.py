"""Strict, public-safe state handoff for deterministic crawler runs.

The crawler bundle deliberately contains only the state required to continue
feed and sitemap discovery on an ephemeral machine.  It never contains local
read/star state, notifications, raw errors, article full text, AI artifacts,
AI jobs, provider identifiers, credentials, or model usage.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .database import Database, utc_now
from .models import SourceConfig
from .normalize import stable_hash
from .sync import _is_expected_article_url


CRAWLER_STATE_PROTOCOL = "aaron-reader-crawler-state-v1"
CRAWLER_STATE_MAX_BYTES = 25 * 1024 * 1024
CRAWLER_STATE_MAX_SOURCES = 100
CRAWLER_STATE_MAX_ARTICLES = 10_000
CRAWLER_STATE_MAX_HTTP_CACHE = 1_000
CRAWLER_STATE_MAX_SEEN_URLS = 100_000
CRAWLER_STATE_MAX_PENDING_URLS = 10_000
CRAWLER_STATE_MAX_SOURCE_CHECKS = 1_000

_TOP_LEVEL_KEYS = {
    "protocol",
    "exported_at",
    "bundle_hash",
    "sources",
    "articles",
    "http_cache",
    "seen_urls",
    "pending_urls",
    "source_checks",
}
_SOURCE_KEYS = {
    "slug",
    "name",
    "home_url",
    "fetch_url",
    "adapter",
    "enabled",
    "etag",
    "last_modified",
    "body_hash",
    "initialized_at",
    "last_checked_at",
    "last_success_at",
    "last_http_status",
    "last_item_count",
    "updated_at",
}
_ARTICLE_KEYS = {
    "source_slug",
    "external_id",
    "canonical_url",
    "title",
    "summary",
    "author",
    "category",
    "published_at",
    "modified_at",
    "discovered_at",
    "updated_at",
    "content_hash",
    "is_backfill",
}
_HTTP_CACHE_KEYS = {
    "url",
    "etag",
    "last_modified",
    "body_hash",
    "last_checked_at",
    "last_status",
}
_SEEN_URL_KEYS = {
    "source_slug",
    "url",
    "remote_modified",
    "first_seen_at",
    "last_seen_at",
}
_PENDING_URL_KEYS = {
    "source_slug",
    "url",
    "remote_modified",
    "change_kind",
    "first_seen_at",
    "last_attempt_at",
    "next_attempt_at",
    "attempt_count",
}
_SOURCE_CHECK_KEYS = {
    "source_slug",
    "check_name",
    "last_checked_at",
    "last_success_at",
    "failure_streak",
}
_HEX = set("0123456789abcdef")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _bundle_hash(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("bundle_hash", None)
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("crawler state contains a duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _read_payload(path: Path) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("crawler state is not a regular file: %s" % source)
    with source.open("rb") as handle:
        raw = handle.read(CRAWLER_STATE_MAX_BYTES + 1)
    if not raw or len(raw) > CRAWLER_STATE_MAX_BYTES:
        raise ValueError("crawler state size is outside the safe range")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("crawler state must be UTF-8 JSON") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("crawler state is not strict JSON: %s" % exc) from exc


def _atomic_write(path: Path, payload: Mapping[str, object]) -> Path:
    destination = Path(path)
    if destination.is_symlink():
        raise ValueError("crawler state destination cannot be a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > CRAWLER_STATE_MAX_BYTES:
        raise ValueError("crawler state exceeds the publication size limit")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return destination


def _rows(connection, query: str, values: Sequence[object] = ()) -> List[Dict[str, object]]:
    return [dict(row) for row in connection.execute(query, tuple(values)).fetchall()]


def export_crawler_state(
    database: Database,
    configured_sources: Sequence[SourceConfig],
    path: Path,
) -> Dict[str, object]:
    """Atomically export the minimal crawler continuation state."""

    source_slugs = [source.slug for source in configured_sources]
    if not source_slugs:
        raise ValueError("crawler state requires at least one configured source")
    placeholders = ",".join("?" for _ in source_slugs)
    with database.connect() as connection:
        sources = _rows(
            connection,
            """
            SELECT slug, name, home_url, fetch_url, adapter, enabled, etag,
                last_modified, body_hash, initialized_at, last_checked_at,
                last_success_at, last_http_status, last_item_count, updated_at
            FROM sources WHERE slug IN (%s) ORDER BY slug
            """ % placeholders,
            source_slugs,
        )
        articles = _rows(
            connection,
            """
            SELECT source_slug, external_id, canonical_url, title, summary,
                author, category, published_at, modified_at, discovered_at,
                updated_at, content_hash, is_backfill
            FROM articles WHERE source_slug IN (%s)
            ORDER BY source_slug, external_id, canonical_url
            """ % placeholders,
            source_slugs,
        )
        http_cache = _rows(
            connection,
            """
            SELECT url, etag, last_modified, body_hash, last_checked_at, last_status
            FROM http_cache ORDER BY url
            """,
        )
        seen_urls = _rows(
            connection,
            """
            SELECT source_slug, url, remote_modified, first_seen_at, last_seen_at
            FROM seen_urls WHERE source_slug IN (%s)
            ORDER BY source_slug, url
            """ % placeholders,
            source_slugs,
        )
        pending_urls = _rows(
            connection,
            """
            SELECT source_slug, url, remote_modified, change_kind, first_seen_at,
                last_attempt_at, next_attempt_at, attempt_count
            FROM pending_urls WHERE source_slug IN (%s)
            ORDER BY source_slug, url
            """ % placeholders,
            source_slugs,
        )
        source_checks = _rows(
            connection,
            """
            SELECT source_slug, check_name, last_checked_at, last_success_at,
                failure_streak
            FROM source_checks WHERE source_slug IN (%s)
            ORDER BY source_slug, check_name
            """ % placeholders,
            source_slugs,
        )

    # Older databases may contain a sitemap section root as a seen marker.
    # It is not a fetchable article cursor, so omit it from the strict public
    # handoff.  Invalid pending URLs are never filtered: validation fails the
    # export because a pending row can drive a later detail request.
    configured_by_slug = {source.slug: source for source in configured_sources}
    safe_seen_urls = []
    for row in seen_urls:
        configured = configured_by_slug[str(row["source_slug"])]
        url = str(row["url"])
        if _source_url_is_allowed(configured, url):
            safe_seen_urls.append(row)
        elif not _is_exact_sitemap_section_root(configured, url):
            raise ValueError("crawler database contains a seen URL outside its source")
    seen_urls = safe_seen_urls

    payload: Dict[str, object] = {
        "protocol": CRAWLER_STATE_PROTOCOL,
        "exported_at": utc_now(),
        "bundle_hash": "",
        "sources": sources,
        "articles": articles,
        "http_cache": http_cache,
        "seen_urls": seen_urls,
        "pending_urls": pending_urls,
        "source_checks": source_checks,
    }
    _validate_payload(payload, configured_sources, verify_hash=False)
    payload["bundle_hash"] = _bundle_hash(payload)
    _validate_payload(payload, configured_sources, verify_hash=True)
    destination = _atomic_write(Path(path), payload)
    return {
        "protocol": CRAWLER_STATE_PROTOCOL,
        "path": str(destination),
        "bundle_hash": payload["bundle_hash"],
        "exported_at": payload["exported_at"],
        "sources": len(sources),
        "articles": len(articles),
        "seen_urls": len(seen_urls),
        "pending_urls": len(pending_urls),
    }


def import_crawler_state(
    database: Database,
    configured_sources: Sequence[SourceConfig],
    path: Path,
    *,
    seed: bool = False,
) -> Dict[str, object]:
    """Strictly import a crawler bundle, preserving every local-only table.

    Merge mode preserves existing article IDs, read/star state, AI artifacts,
    jobs, attempts, notifications, and full-text snapshots.  New
    articles are inserted unread.  Seed mode requires an empty content/AI
    database and marks imported history read so a subsequent sync identifies
    only genuinely new discoveries.
    """

    payload = _read_payload(Path(path))
    validated = _validate_payload(payload, configured_sources, verify_hash=True)
    exported_at = str(validated["exported_at"])
    configured_by_slug = {source.slug: source for source in configured_sources}
    bundle_sources = {
        str(item["slug"]): item for item in validated["sources"]  # type: ignore[index]
    }
    bundle_seen = _group_by_source(validated["seen_urls"])  # type: ignore[arg-type]
    bundle_pending = _group_by_source(validated["pending_urls"])  # type: ignore[arg-type]
    bundle_checks = _group_by_source(validated["source_checks"])  # type: ignore[arg-type]

    inserted = 0
    updated = 0
    unchanged = 0
    authoritative_sources = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if seed:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM articles) AS articles,
                    (SELECT COUNT(*) FROM ai_artifacts) AS artifacts,
                    (SELECT COUNT(*) FROM article_content_snapshots) AS snapshots
                """
            ).fetchone()
            if any(int(counts[key] or 0) for key in counts.keys()):
                connection.rollback()
                raise ValueError("--seed requires an empty article and AI database")

        for slug, bundle_source in bundle_sources.items():
            local = connection.execute(
                "SELECT * FROM sources WHERE slug=?", (slug,)
            ).fetchone()
            if local is None:
                connection.rollback()
                raise ValueError("configured source disappeared during crawler import")
            config_source = configured_by_slug[slug]
            identity = (
                str(local["home_url"]),
                str(local["fetch_url"]),
                str(local["adapter"]),
            )
            expected_identity = (
                config_source.home_url,
                config_source.fetch_url,
                config_source.adapter,
            )
            if identity != expected_identity:
                connection.rollback()
                raise ValueError("local source identity differs from current configuration")
            incoming_checked = str(bundle_source.get("last_checked_at") or "")
            local_checked = str(local["last_checked_at"] or "")
            authoritative = seed or not local_checked or incoming_checked >= local_checked
            if not authoritative:
                continue
            authoritative_sources += 1
            connection.execute(
                """
                UPDATE sources SET name=?, enabled=?, etag=?, last_modified=?,
                    body_hash=?, initialized_at=?, last_checked_at=?,
                    last_success_at=?, failure_streak=0, last_error='',
                    last_http_status=?, last_item_count=?, updated_at=?
                WHERE slug=?
                """,
                (
                    bundle_source["name"],
                    int(bool(bundle_source["enabled"])),
                    bundle_source["etag"],
                    bundle_source["last_modified"],
                    bundle_source["body_hash"],
                    bundle_source["initialized_at"],
                    bundle_source["last_checked_at"],
                    bundle_source["last_success_at"],
                    bundle_source["last_http_status"],
                    bundle_source["last_item_count"],
                    bundle_source["updated_at"],
                    slug,
                ),
            )
            connection.execute("DELETE FROM seen_urls WHERE source_slug=?", (slug,))
            connection.execute("DELETE FROM pending_urls WHERE source_slug=?", (slug,))
            connection.execute("DELETE FROM source_checks WHERE source_slug=?", (slug,))
            _insert_seen_rows(connection, bundle_seen.get(slug, []))
            _insert_pending_rows(connection, bundle_pending.get(slug, []))
            _insert_check_rows(connection, bundle_checks.get(slug, []))

        allowed_cache_urls = {
            str(source.sitemap_url)
            for source in configured_sources
            if source.sitemap_url
        }
        for cache in validated["http_cache"]:  # type: ignore[index]
            url = str(cache["url"])
            if url not in allowed_cache_urls:
                connection.rollback()
                raise ValueError("crawler state contains an unconfigured HTTP cache URL")
            local = connection.execute(
                "SELECT last_checked_at FROM http_cache WHERE url=?", (url,)
            ).fetchone()
            if local is not None and not seed and str(local["last_checked_at"] or "") > str(
                cache["last_checked_at"]
            ):
                continue
            connection.execute(
                """
                INSERT INTO http_cache(
                    url, etag, last_modified, body_hash, last_checked_at, last_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag=excluded.etag,
                    last_modified=excluded.last_modified,
                    body_hash=excluded.body_hash,
                    last_checked_at=excluded.last_checked_at,
                    last_status=excluded.last_status
                """,
                tuple(cache[key] for key in (
                    "url", "etag", "last_modified", "body_hash",
                    "last_checked_at", "last_status",
                )),
            )

        for article in validated["articles"]:  # type: ignore[index]
            slug = str(article["source_slug"])
            matches = connection.execute(
                """
                SELECT * FROM articles
                WHERE source_slug=? AND (external_id=? OR canonical_url=?)
                ORDER BY id
                """,
                (slug, article["external_id"], article["canonical_url"]),
            ).fetchall()
            if len(matches) > 1:
                connection.rollback()
                raise ValueError("crawler article identity maps to multiple local rows")
            if not matches:
                connection.execute(
                    """
                    INSERT INTO articles(
                        source_slug, external_id, canonical_url, title, summary,
                        author, category, published_at, modified_at, discovered_at,
                        updated_at, content_hash, is_backfill, read_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        article["external_id"],
                        article["canonical_url"],
                        article["title"],
                        article["summary"],
                        article["author"],
                        article["category"],
                        article["published_at"],
                        article["modified_at"],
                        article["discovered_at"],
                        article["updated_at"],
                        article["content_hash"],
                        int(bool(article["is_backfill"])),
                        exported_at if seed else None,
                    ),
                )
                inserted += 1
                continue
            local = matches[0]
            if not seed and str(local["updated_at"] or "") > str(article["updated_at"]):
                unchanged += 1
                continue
            changed = any(
                local[key] != article[key]
                for key in (
                    "external_id", "canonical_url", "title", "summary", "author",
                    "category", "published_at", "modified_at", "discovered_at",
                    "updated_at", "content_hash", "is_backfill",
                )
            )
            if not changed:
                unchanged += 1
                continue
            connection.execute(
                """
                UPDATE articles SET external_id=?, canonical_url=?, title=?,
                    summary=?, author=?, category=?, published_at=?, modified_at=?,
                    discovered_at=?, updated_at=?, content_hash=?, is_backfill=?
                WHERE id=?
                """,
                (
                    article["external_id"],
                    article["canonical_url"],
                    article["title"],
                    article["summary"],
                    article["author"],
                    article["category"],
                    article["published_at"],
                    article["modified_at"],
                    article["discovered_at"],
                    article["updated_at"],
                    article["content_hash"],
                    int(bool(article["is_backfill"])),
                    int(local["id"]),
                ),
            )
            updated += 1
        connection.commit()

    return {
        "protocol": CRAWLER_STATE_PROTOCOL,
        "path": str(Path(path)),
        "bundle_hash": validated["bundle_hash"],
        "mode": "seed" if seed else "merge",
        "sources": len(bundle_sources),
        "authoritative_sources": authoritative_sources,
        "articles": len(validated["articles"]),  # type: ignore[arg-type]
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "ai_artifacts_touched": 0,
        "personal_state_touched": 0,
    }


def _group_by_source(
    rows: Sequence[Mapping[str, object]],
) -> Dict[str, List[Mapping[str, object]]]:
    grouped: Dict[str, List[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["source_slug"]), []).append(row)
    return grouped


def _insert_seen_rows(connection, rows: Sequence[Mapping[str, object]]) -> None:
    connection.executemany(
        """
        INSERT INTO seen_urls(
            source_slug, url, remote_modified, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            tuple(row[key] for key in (
                "source_slug", "url", "remote_modified", "first_seen_at", "last_seen_at",
            ))
            for row in rows
        ],
    )


def _insert_pending_rows(connection, rows: Sequence[Mapping[str, object]]) -> None:
    connection.executemany(
        """
        INSERT INTO pending_urls(
            source_slug, url, remote_modified, change_kind, first_seen_at,
            last_attempt_at, next_attempt_at, attempt_count, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
        """,
        [
            tuple(row[key] for key in (
                "source_slug", "url", "remote_modified", "change_kind", "first_seen_at",
                "last_attempt_at", "next_attempt_at", "attempt_count",
            ))
            for row in rows
        ],
    )


def _insert_check_rows(connection, rows: Sequence[Mapping[str, object]]) -> None:
    connection.executemany(
        """
        INSERT INTO source_checks(
            source_slug, check_name, last_checked_at, last_success_at,
            failure_streak, last_error
        ) VALUES (?, ?, ?, ?, ?, '')
        """,
        [
            tuple(row[key] for key in (
                "source_slug", "check_name", "last_checked_at", "last_success_at",
                "failure_streak",
            ))
            for row in rows
        ],
    )


def _validate_payload(
    payload: object,
    configured_sources: Sequence[SourceConfig],
    *,
    verify_hash: bool,
) -> Mapping[str, object]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("crawler state top-level fields do not match the contract")
    if payload.get("protocol") != CRAWLER_STATE_PROTOCOL:
        raise ValueError("unsupported crawler state protocol")
    _timestamp(payload.get("exported_at"), "exported_at", nullable=False)
    if verify_hash:
        _sha256(payload.get("bundle_hash"), "bundle_hash", allow_empty=False)
        if payload.get("bundle_hash") != _bundle_hash(payload):
            raise ValueError("crawler state bundle hash does not match its contents")

    collection_specs = (
        ("sources", CRAWLER_STATE_MAX_SOURCES, _SOURCE_KEYS),
        ("articles", CRAWLER_STATE_MAX_ARTICLES, _ARTICLE_KEYS),
        ("http_cache", CRAWLER_STATE_MAX_HTTP_CACHE, _HTTP_CACHE_KEYS),
        ("seen_urls", CRAWLER_STATE_MAX_SEEN_URLS, _SEEN_URL_KEYS),
        ("pending_urls", CRAWLER_STATE_MAX_PENDING_URLS, _PENDING_URL_KEYS),
        ("source_checks", CRAWLER_STATE_MAX_SOURCE_CHECKS, _SOURCE_CHECK_KEYS),
    )
    for name, maximum, keys in collection_specs:
        value = payload.get(name)
        if not isinstance(value, list) or len(value) > maximum:
            raise ValueError("crawler state %s collection is invalid" % name)
        for row in value:
            if not isinstance(row, dict) or set(row) != keys:
                raise ValueError("crawler state %s fields do not match the contract" % name)

    configured_by_slug = {source.slug: source for source in configured_sources}
    raw_sources = payload["sources"]
    if len(configured_by_slug) != len(configured_sources):
        raise ValueError("configured crawler source slugs are not unique")
    if len(raw_sources) != len(configured_by_slug):
        raise ValueError("crawler state source set differs from current configuration")
    seen_source_slugs = set()
    for row in raw_sources:
        slug = _string(row["slug"], "source slug", 120, allow_empty=False)
        if slug in seen_source_slugs or slug not in configured_by_slug:
            raise ValueError("crawler state source slug is unknown or duplicated")
        seen_source_slugs.add(slug)
        configured = configured_by_slug[slug]
        expected_identity = (
            configured.name,
            configured.home_url,
            configured.fetch_url,
            configured.adapter,
            bool(configured.enabled),
        )
        actual_identity = (
            row["name"], row["home_url"], row["fetch_url"], row["adapter"],
            bool(row["enabled"]),
        )
        if type(row["enabled"]) not in (bool, int) or int(row["enabled"]) not in (0, 1):
            raise ValueError("crawler state source enabled flag must be boolean")
        if actual_identity != expected_identity:
            raise ValueError("crawler state source identity differs from configuration")
        _http_url(row["home_url"], "source home_url")
        _http_url(row["fetch_url"], "source fetch_url")
        _string(row["etag"], "source etag", 2_000)
        _string(row["last_modified"], "source last_modified", 2_000)
        _sha256(row["body_hash"], "source body_hash", allow_empty=True)
        for key in ("initialized_at", "last_checked_at", "last_success_at"):
            _timestamp(row[key], "source %s" % key, nullable=True)
        _optional_http_status(row["last_http_status"], "source last_http_status")
        _integer(row["last_item_count"], "source last_item_count", 0, 1_000_000)
        _timestamp(row["updated_at"], "source updated_at", nullable=False)
    if seen_source_slugs != set(configured_by_slug):
        raise ValueError("crawler state source set differs from current configuration")

    article_identities = set()
    article_urls = set()
    for row in payload["articles"]:
        slug = _known_slug(row["source_slug"], configured_by_slug)
        configured_source = configured_by_slug[slug]
        external_id = _string(
            row["external_id"], "article external_id", 1_000, allow_empty=False
        )
        url = _http_url(row["canonical_url"], "article canonical_url")
        if not _source_url_is_allowed(configured_source, url):
            raise ValueError("crawler article URL is outside its configured source")
        if (slug, external_id) in article_identities or (slug, url) in article_urls:
            raise ValueError("crawler state article identity is duplicated")
        article_identities.add((slug, external_id))
        article_urls.add((slug, url))
        title = _string(row["title"], "article title", 500, allow_empty=False)
        summary = _string(row["summary"], "article summary", 20_000)
        author = _string(row["author"], "article author", 200)
        category = _string(row["category"], "article category", 120)
        published_at = _timestamp(
            row["published_at"], "article published_at", nullable=True
        )
        modified_at = _timestamp(
            row["modified_at"], "article modified_at", nullable=True
        )
        _timestamp(row["discovered_at"], "article discovered_at", nullable=False)
        _timestamp(row["updated_at"], "article updated_at", nullable=False)
        content_hash = _sha256(
            row["content_hash"], "article content_hash", allow_empty=False
        )
        expected_hash = stable_hash(
            url, title, summary, author, category, published_at, modified_at
        )
        if content_hash != expected_hash:
            raise ValueError("crawler article content hash does not match its metadata")
        if type(row["is_backfill"]) not in (bool, int) or int(
            row["is_backfill"]
        ) not in (0, 1):
            raise ValueError("crawler article is_backfill must be boolean")

    allowed_cache_urls = {
        str(source.sitemap_url)
        for source in configured_sources
        if source.sitemap_url
    }
    cache_urls = set()
    for row in payload["http_cache"]:
        url = _http_url(row["url"], "HTTP cache URL")
        if url in cache_urls or url not in allowed_cache_urls:
            raise ValueError("crawler HTTP cache URL is unknown or duplicated")
        cache_urls.add(url)
        _string(row["etag"], "HTTP cache etag", 2_000)
        _string(row["last_modified"], "HTTP cache last_modified", 2_000)
        _sha256(row["body_hash"], "HTTP cache body_hash", allow_empty=True)
        _timestamp(row["last_checked_at"], "HTTP cache last_checked_at", nullable=False)
        _optional_http_status(row["last_status"], "HTTP cache last_status")

    seen_identities = set()
    for row in payload["seen_urls"]:
        slug = _known_slug(row["source_slug"], configured_by_slug)
        url = _http_url(row["url"], "seen URL")
        if not _source_url_is_allowed(configured_by_slug[slug], url):
            raise ValueError("crawler seen URL is outside its configured source")
        if (slug, url) in seen_identities:
            raise ValueError("crawler seen URL is duplicated")
        seen_identities.add((slug, url))
        _string(row["remote_modified"], "seen remote_modified", 2_000)
        _timestamp(row["first_seen_at"], "seen first_seen_at", nullable=False)
        _timestamp(row["last_seen_at"], "seen last_seen_at", nullable=False)

    pending_identities = set()
    for row in payload["pending_urls"]:
        slug = _known_slug(row["source_slug"], configured_by_slug)
        url = _http_url(row["url"], "pending URL")
        if not _source_url_is_allowed(configured_by_slug[slug], url):
            raise ValueError("crawler pending URL is outside its configured source")
        if (slug, url) in pending_identities:
            raise ValueError("crawler pending URL is duplicated")
        pending_identities.add((slug, url))
        _string(row["remote_modified"], "pending remote_modified", 2_000)
        if row["change_kind"] not in ("new", "modified"):
            raise ValueError("crawler pending URL change_kind is invalid")
        _timestamp(row["first_seen_at"], "pending first_seen_at", nullable=False)
        _timestamp(row["last_attempt_at"], "pending last_attempt_at", nullable=True)
        _timestamp(row["next_attempt_at"], "pending next_attempt_at", nullable=True)
        _integer(row["attempt_count"], "pending attempt_count", 0, 1_000_000)

    check_identities = set()
    for row in payload["source_checks"]:
        slug = _known_slug(row["source_slug"], configured_by_slug)
        name = _string(row["check_name"], "source check name", 120, allow_empty=False)
        if (slug, name) in check_identities:
            raise ValueError("crawler source check is duplicated")
        check_identities.add((slug, name))
        _timestamp(row["last_checked_at"], "check last_checked_at", nullable=False)
        _timestamp(row["last_success_at"], "check last_success_at", nullable=True)
        _integer(row["failure_streak"], "check failure_streak", 0, 1_000_000)
    return payload


def _known_slug(value: object, configured: Mapping[str, SourceConfig]) -> str:
    slug = _string(value, "source slug", 120, allow_empty=False)
    if slug not in configured:
        raise ValueError("crawler state references an unknown source slug")
    return slug


def _source_url_is_allowed(source: SourceConfig, url: str) -> bool:
    """Bind every imported fetchable URL to its configured publisher.

    Sitemap-backed sources use the same one-slug-below-prefix rule as live
    hydration.  Listing-only sources cannot express one universal path shape,
    so their URLs are restricted to the configured publisher hosts.
    """

    if source.sitemap_prefix:
        return _is_expected_article_url(source, url)
    candidate_host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    allowed_hosts = {
        (urlsplit(configured_url).hostname or "").lower().removeprefix("www.")
        for configured_url in (
            source.home_url,
            source.fetch_url,
            source.metadata_url,
        )
        if configured_url
    }
    return bool(candidate_host) and candidate_host in allowed_hosts


def _is_exact_sitemap_section_root(source: SourceConfig, url: str) -> bool:
    if not source.sitemap_prefix:
        return False
    candidate = urlsplit(url)
    prefix = urlsplit(source.sitemap_prefix)
    candidate_host = (candidate.hostname or "").lower().removeprefix("www.")
    prefix_host = (prefix.hostname or "").lower().removeprefix("www.")
    return (
        candidate.scheme in ("http", "https")
        and candidate_host == prefix_host
        and candidate.path.rstrip("/") == prefix.path.rstrip("/")
        and not candidate.query
        and not candidate.fragment
    )


def _string(
    value: object,
    field: str,
    maximum: int,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ValueError("crawler state %s is invalid" % field)
    for character in value:
        codepoint = ord(character)
        if codepoint == 0 or 0x7F <= codepoint <= 0x9F:
            raise ValueError("crawler state %s contains control characters" % field)
    return value


def _http_url(value: object, field: str) -> str:
    url = _string(value, field, 4_000, allow_empty=False)
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError("crawler state %s is not a valid URL" % field) from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("crawler state %s must be an HTTP(S) URL" % field)
    if parts.username or parts.password or parts.fragment:
        raise ValueError("crawler state %s contains forbidden URL components" % field)
    return url


def _timestamp(value: object, field: str, *, nullable: bool) -> Optional[str]:
    if value is None and nullable:
        return None
    text = _string(value, field, 40, allow_empty=False)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("crawler state %s is not an ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("crawler state %s must use UTC" % field)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(value: object, field: str, *, allow_empty: bool) -> str:
    text = _string(value, field, 64, allow_empty=allow_empty)
    if not text and allow_empty:
        return ""
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError("crawler state %s must be a SHA-256 hex string" % field)
    return text


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError("crawler state %s is outside the safe range" % field)
    return value


def _optional_http_status(value: object, field: str) -> Optional[int]:
    if value is None:
        return None
    return _integer(value, field, 100, 599)
