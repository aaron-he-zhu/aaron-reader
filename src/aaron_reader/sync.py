import errno
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from .database import Database, utc_now
from .http_client import FetchError, HttpClient
from .i18n import translate
from .models import AppConfig, ArticleCandidate, SourceConfig, SourceSyncResult, SyncResult
from .parsers import parse_article_page, parse_cursor_zh_locale, parse_sitemap, parse_source
from .normalize import stable_hash


class SyncAlreadyRunning(RuntimeError):
    pass


@contextmanager
def sync_lock(lock_path: Path, language: str = "en") -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SyncAlreadyRunning(translate("sync.locked", language))
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def sync_all(
    config: AppConfig,
    database: Database,
    source_slugs: Optional[Sequence[str]] = None,
    force: bool = False,
    keep_history_unread: bool = False,
    client: Optional[HttpClient] = None,
    language: str = "en",
) -> SyncResult:
    selected = set(source_slugs or [])
    known = {source.slug for source in config.sources}
    unknown = selected - known
    if unknown:
        raise ValueError(
            translate(
                "sync.unknown_sources",
                language,
                sources=", ".join(sorted(unknown)),
            )
        )
    disabled = {
        source.slug for source in config.sources if source.slug in selected and not source.enabled
    }
    if disabled:
        raise ValueError(
            translate(
                "sync.disabled_sources",
                language,
                sources=", ".join(sorted(disabled)),
            )
        )

    http = client or HttpClient(
        timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=config.max_response_bytes,
    )
    results = SyncResult()
    lock_path = database.path.with_suffix(database.path.suffix + ".sync.lock")
    with sync_lock(lock_path, language=language):
        database.initialize()
        database.sync_source_configs(config.sources, language=language)
        for source in config.sources:
            if not source.enabled or (selected and source.slug not in selected):
                continue
            result = _sync_source(
                source,
                database,
                http,
                force=force,
                keep_history_unread=keep_history_unread,
                language=language,
            )
            results.sources.append(result)
            if source.zh_locale_url and result.status not in ("error",):
                sync_publisher_locale(source, database, http, language=language)

    return results


def _sync_source(
    source: SourceConfig,
    database: Database,
    client: HttpClient,
    force: bool,
    keep_history_unread: bool,
    language: str = "en",
) -> SourceSyncResult:
    started_at = utc_now()
    state = database.source_state(source.slug)
    try:
        direct_pending_before = (
            database.pending_url_count(source.slug) if not source.sitemap_url else 0
        )
        missing_developer_dates = (
            source.adapter == "openai_developers"
            and database.source_has_missing_dates(source.slug)
        )
        needs_primary_body = force or direct_pending_before > 0 or missing_developer_dates
        response = client.fetch(
            source.fetch_url,
            etag="" if needs_primary_body else str(state.get("etag") or ""),
            last_modified="" if needs_primary_body else str(state.get("last_modified") or ""),
        )
        primary_mode = "parsed"
        if response.not_modified:
            primary_mode = "not_modified"
        elif (
            not needs_primary_body
            and state.get("initialized_at")
            and state.get("body_hash")
            and response.body_hash == state.get("body_hash")
        ):
            primary_mode = "unchanged"

        listing_candidates: List[ArticleCandidate] = []
        listing_item_count: Optional[int] = None
        parsed: List[ArticleCandidate] = []
        metadata_warnings: List[str] = []
        if primary_mode == "parsed":
            parsed = parse_source(
                source,
                response.body,
                fetched_at=datetime.now(timezone.utc),
            )
            if not parsed:
                raise ValueError(translate("sync.empty_parse", language))
            if source.adapter == "openai_developers":
                parsed, metadata_warnings = _hydrate_openai_developer_dates(
                    source, parsed, database, client, language=language
                )
                if metadata_warnings:
                    database.record_check_failure(
                        source.slug, "metadata", "; ".join(metadata_warnings)
                    )
                else:
                    database.record_check_success(source.slug, "metadata")
            listing_candidates = _select_history(parsed, source.history_limit)
            listing_item_count = len(listing_candidates)
            previous_count = int(state.get("last_item_count") or 0)
            if state.get("initialized_at") and previous_count >= 10:
                drift_floor = max(3, previous_count // 5)
                if len(listing_candidates) < drift_floor:
                    raise ValueError(
                        translate(
                            "sync.parser_drift",
                            language,
                            previous=previous_count,
                            current=len(listing_candidates),
                        )
                    )

        warnings: List[str] = list(metadata_warnings)
        direct_entries_to_mark: List[Tuple[str, Optional[str]]] = []
        if primary_mode == "parsed" and not source.sitemap_url:
            discovery_entries = [(item.url, item.published_at) for item in parsed]
            database.discover_sitemap_urls(source.slug, discovery_entries)
            selected_urls = {item.url for item in listing_candidates}
            parsed_by_url = {item.url: item for item in parsed}
            for pending in database.pending_urls(source.slug, 200):
                url = str(pending["url"])
                remote_modified = str(pending.get("remote_modified") or "") or None
                candidate = parsed_by_url.get(url)
                if candidate is None:
                    message = translate("sync.pending_missing", language, url=url)
                    database.record_pending_failure(source.slug, url, message)
                    warnings.append(message)
                    continue
                if url not in selected_urls:
                    listing_candidates.append(candidate)
                    selected_urls.add(url)
                direct_entries_to_mark.append((url, remote_modified))
            remaining_direct = max(
                0,
                database.pending_url_count(source.slug) - len(direct_entries_to_mark),
            )
            if remaining_direct:
                warnings.append(
                    translate("sync.direct_remaining", language, count=remaining_direct)
                )

        hydrated_candidates: List[ArticleCandidate] = []
        sitemap_entries_to_mark: List[Tuple[str, Optional[str]]] = []
        hydration_failed = False
        if source.sitemap_url and (force or database.sitemap_is_due(source.sitemap_url, source.sitemap_interval_hours)):
            try:
                cache = database.http_cache(source.sitemap_url) or {}
                sitemap_response = client.fetch(
                    source.sitemap_url,
                    etag="" if force else str(cache.get("etag") or ""),
                    last_modified="" if force else str(cache.get("last_modified") or ""),
                )
                sitemap_changed = not sitemap_response.not_modified and (
                    force
                    or not cache.get("body_hash")
                    or sitemap_response.body_hash != cache.get("body_hash")
                )
                if sitemap_changed:
                    entries = parse_sitemap(sitemap_response.body, source.sitemap_prefix)
                    entries = [
                        entry
                        for entry in entries
                        if _is_expected_article_url(source, entry[0])
                    ]
                    if not entries:
                        raise ValueError(
                            translate(
                                "sync.sitemap_empty",
                                language,
                                prefix=source.sitemap_prefix,
                            )
                        )
                    database.discover_sitemap_urls(source.slug, entries)
                database.record_http_cache(
                    source.sitemap_url,
                    sitemap_response.status,
                    sitemap_response.etag,
                    sitemap_response.last_modified,
                    sitemap_response.body_hash,
                    not_modified=sitemap_response.not_modified,
                )
                database.record_check_success(source.slug, "sitemap")
            except Exception as exc:
                message = translate("sync.sitemap_failed", language, error=exc)
                warnings.append(message)
                database.record_check_failure(source.slug, "sitemap", str(exc))

        if source.sitemap_url:
            listing_by_url = {item.url: item for item in listing_candidates}
            for pending in database.pending_urls(source.slug, 25):
                url = str(pending["url"])
                remote_modified = str(pending.get("remote_modified") or "") or None
                change_kind = str(pending.get("change_kind") or "new")
                existing = database.article_by_url(source.slug, url)
                if not _is_expected_article_url(source, url):
                    warnings.append(
                        translate("sync.invalid_sitemap_url", language, url=url)
                    )
                    sitemap_entries_to_mark.append((url, remote_modified))
                    continue
                if existing is not None and change_kind == "new":
                    # The high-frequency listing may already have inserted this URL
                    # before the daily sitemap first observed it.
                    sitemap_entries_to_mark.append((url, remote_modified))
                    continue
                if existing is None and change_kind == "modified":
                    # This was historical sitemap-only state, not a newly published
                    # article.  Advancing lastmod must not create a false notification.
                    sitemap_entries_to_mark.append((url, remote_modified))
                    continue
                listing_candidate = listing_by_url.get(url)
                if listing_candidate is not None and change_kind == "new":
                    hydrated_candidates.append(listing_candidate)
                    sitemap_entries_to_mark.append((url, remote_modified))
                    continue
                try:
                    article_response = client.fetch(url, attempts=2)
                    if not _is_expected_article_url(source, article_response.url):
                        raise ValueError(
                            translate(
                                "sync.redirect_invalid",
                                language,
                                url=article_response.url,
                            )
                        )
                    article = parse_article_page(
                        source,
                        article_response.body,
                        article_response.url,
                        fetched_at=datetime.now(timezone.utc),
                    )
                    if not _is_expected_article_url(source, article.url):
                        raise ValueError(
                            translate("sync.canonical_invalid", language, url=article.url)
                        )
                    hydrated_candidates.append(article)
                    sitemap_entries_to_mark.append((url, remote_modified))
                except Exception as exc:
                    hydration_failed = True
                    retry_after = (
                        exc.retry_after_seconds if isinstance(exc, FetchError) else None
                    )
                    database.record_pending_failure(
                        source.slug,
                        url,
                        str(exc),
                        retry_after_seconds=retry_after,
                    )
                    message = translate(
                        "sync.hydration_failed", language, url=url, error=exc
                    )
                    warnings.append(message)
                    database.record_check_failure(source.slug, "hydration", message)
                    if isinstance(exc, FetchError) and exc.status == 429:
                        warnings.append(translate("sync.rate_limited", language))
                        break

            remaining_sitemap = max(
                0,
                database.pending_url_count(source.slug) - len(sitemap_entries_to_mark),
            )
            if remaining_sitemap:
                warnings.append(
                    translate(
                        "sync.sitemap_remaining",
                        language,
                        count=remaining_sitemap,
                    )
                )

        # Detail-page metadata is authoritative for sitemap refreshes.  Put it
        # first so URL de-duplication cannot let a shorter listing card mask it.
        candidates = _deduplicate_candidates(hydrated_candidates + listing_candidates)
        if not candidates:
            database.mark_seen_urls(source.slug, sitemap_entries_to_mark)
            if primary_mode == "not_modified":
                database.record_not_modified(
                    source.slug,
                    response.status,
                    response.etag,
                    response.last_modified,
                )
            else:
                database.record_unchanged(
                    source.slug,
                    started_at,
                    response.status,
                    response.etag or str(state.get("etag") or ""),
                    response.last_modified or str(state.get("last_modified") or ""),
                    response.body_hash or str(state.get("body_hash") or ""),
                )
            if source.sitemap_url and not hydration_failed and not database.pending_url_count(source.slug):
                database.record_check_success(source.slug, "hydration")
            return SourceSyncResult(
                source_slug=source.slug,
                status="degraded" if warnings else primary_mode,
                http_status=response.status,
                warning="; ".join(warnings),
            )

        inserted, updated, seeded, baseline, new_ids = database.commit_candidates(
            source,
            candidates,
            started_at=started_at,
            http_status=response.status,
            etag=response.etag or str(state.get("etag") or ""),
            last_modified=response.last_modified or str(state.get("last_modified") or ""),
            body_hash=response.body_hash or str(state.get("body_hash") or ""),
            force_history_unread=keep_history_unread,
            listing_item_count=listing_item_count,
        )
        database.mark_seen_urls(
            source.slug, direct_entries_to_mark + sitemap_entries_to_mark
        )
        if source.sitemap_url and not hydration_failed and not database.pending_url_count(source.slug):
            database.record_check_success(source.slug, "hydration")
        return SourceSyncResult(
            source_slug=source.slug,
            status="degraded" if warnings else "ok",
            http_status=response.status,
            discovered=len(candidates),
            inserted=inserted,
            updated=updated,
            unread_new=0 if baseline else len(new_ids),
            seeded=seeded,
            warning="; ".join(warnings),
        )
    except Exception as exc:
        status = exc.status if isinstance(exc, FetchError) else None
        database.record_failure(source.slug, started_at, str(exc), status)
        return SourceSyncResult(
            source_slug=source.slug,
            status="error",
            http_status=status,
            error=str(exc),
        )


def _select_history(
    candidates: Sequence[ArticleCandidate], history_limit: int
) -> List[ArticleCandidate]:
    dated = [item for item in candidates if item.published_at]
    undated = [item for item in candidates if not item.published_at]
    dated.sort(key=lambda item: item.published_at or "", reverse=True)
    return (dated + undated)[:history_limit]


def _deduplicate_candidates(candidates: Sequence[ArticleCandidate]) -> List[ArticleCandidate]:
    deduplicated = []
    seen = set()
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        deduplicated.append(candidate)
    return deduplicated


_RESERVED_SECTION_SLUGS = {"blog", "category", "news", "page", "tag", "topic"}


def _is_expected_article_url(source: SourceConfig, url: str) -> bool:
    """Accept only one slug directly below the configured sitemap section."""

    if not source.sitemap_prefix:
        return False
    candidate = urlsplit(url)
    prefix = urlsplit(source.sitemap_prefix)
    candidate_host = (candidate.hostname or "").lower().removeprefix("www.")
    prefix_host = (prefix.hostname or "").lower().removeprefix("www.")
    if candidate.scheme not in ("http", "https") or candidate_host != prefix_host:
        return False
    candidate_parts = [part for part in unquote(candidate.path).split("/") if part]
    prefix_parts = [part for part in unquote(prefix.path).split("/") if part]
    if len(candidate_parts) != len(prefix_parts) + 1:
        return False
    if candidate_parts[: len(prefix_parts)] != prefix_parts:
        return False
    slug = candidate_parts[-1].strip().lower()
    return bool(slug) and slug not in _RESERVED_SECTION_SLUGS


def _hydrate_openai_developer_dates(
    source: SourceConfig,
    candidates: Sequence[ArticleCandidate],
    database: Database,
    client: HttpClient,
    language: str = "en",
) -> tuple:
    """Fetch the HTML listing only when the 4.7 KB Markdown index changes.

    The Markdown index is ideal for cheap discovery but omits dates.  One HTML
    listing request supplies dates for every row, avoiding one request per post.
    """

    if not source.metadata_url:
        return list(candidates), []
    warnings: List[str] = []
    needs_metadata = []
    for candidate in candidates:
        if candidate.published_at:
            continue
        existing = database.article_by_url(source.slug, candidate.url)
        if existing and existing.get("published_at"):
            candidate.published_at = str(existing["published_at"])
            candidate.modified_at = candidate.modified_at or (
                str(existing["modified_at"]) if existing.get("modified_at") else None
            )
            candidate.author = candidate.author or str(existing.get("author") or "")
            candidate.category = candidate.category or str(existing.get("category") or "")
            candidate.content_hash = stable_hash(
                candidate.url,
                candidate.title,
                candidate.summary,
                candidate.author,
                candidate.category,
                candidate.published_at,
                candidate.modified_at,
            )
            continue
        needs_metadata.append(candidate)

    if not needs_metadata:
        return list(candidates), warnings
    try:
        response = client.fetch(
            source.metadata_url,
            attempts=2,
            accept="text/html, application/xhtml+xml;q=0.9, */*;q=0.1",
        )
        metadata_source = SourceConfig(
            slug=source.slug,
            name=source.name,
            home_url=source.home_url,
            fetch_url=source.metadata_url,
            adapter=source.adapter,
            history_limit=source.history_limit,
            enabled=source.enabled,
        )
        metadata_rows = parse_source(
            metadata_source,
            response.body,
            fetched_at=datetime.now(timezone.utc),
        )
        metadata_by_url = {item.url: item for item in metadata_rows}
        for candidate in needs_metadata:
            metadata = metadata_by_url.get(candidate.url)
            if metadata is None:
                warnings.append(
                    translate(
                        "sync.metadata_missing",
                        language,
                        url=candidate.url,
                    )
                )
                continue
            candidate.title = metadata.title or candidate.title
            candidate.summary = candidate.summary or metadata.summary
            candidate.author = metadata.author or candidate.author
            candidate.category = metadata.category or candidate.category
            candidate.published_at = metadata.published_at
            candidate.modified_at = metadata.modified_at
            candidate.content_hash = stable_hash(
                candidate.url,
                candidate.title,
                candidate.summary,
                candidate.author,
                candidate.category,
                candidate.published_at,
                candidate.modified_at,
            )
    except Exception as exc:
        warnings.append(translate("sync.metadata_failed", language, error=exc))
    return list(candidates), warnings


def sync_publisher_locale(
    source: SourceConfig,
    database: Database,
    client: HttpClient,
    language: str = "en",
) -> Dict[str, object]:
    """Fetch official Chinese locale and store as publisher-provided translations.

    For sources with zh_locale_url configured, fetches the official Chinese page
    and stores title/summary as translation artifacts with provider='publisher'.
    These artifacts skip AI model calls during cloud-run.
    """
    if not source.zh_locale_url:
        return {"skipped": True, "reason": "no_zh_locale_url"}

    try:
        response = client.fetch(
            source.zh_locale_url,
            attempts=2,
            accept="text/html, application/xhtml+xml;q=0.9, */*;q=0.1",
        )
        zh_metadata = parse_cursor_zh_locale(source, response.body)
        if not zh_metadata:
            return {
                "skipped": False,
                "zh_locale_url": source.zh_locale_url,
                "articles_with_zh": 0,
                "publisher_locales_stored": 0,
            }

        stored = 0
        with database.connect() as connection:
            articles = connection.execute(
                """
                SELECT id, canonical_url, content_hash
                FROM articles
                WHERE source_slug=?
                """,
                (source.slug,),
            ).fetchall()

        for article in articles:
            url = str(article["canonical_url"])
            slug = url.rsplit("/", 1)[-1].lower()
            zh = zh_metadata.get(slug)
            if zh is None:
                continue
            database.upsert_publisher_locale(
                article_id=int(article["id"]),
                target_language="zh-CN",
                title=zh["title"],
                summary=zh["summary"],
                article_content_hash=str(article["content_hash"]),
            )
            stored += 1

        return {
            "skipped": False,
            "zh_locale_url": source.zh_locale_url,
            "articles_with_zh": len(zh_metadata),
            "publisher_locales_stored": stored,
        }
    except Exception as exc:
        return {
            "skipped": False,
            "zh_locale_url": source.zh_locale_url,
            "error": str(exc)[:200],
        }
