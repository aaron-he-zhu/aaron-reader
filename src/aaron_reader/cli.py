import argparse
import json
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .ai_profiles import (
    DEFAULT_AI_FALLBACK_PROVIDER,
    DEFAULT_AI_PROVIDER,
    SUPPORTED_AI_PROVIDERS,
    ai_provider_profile,
)
from .config import load_config, resolve_project_path
from .crawler_state import export_crawler_state, import_crawler_state
from .database import Database
from .http_client import HttpClient
from .i18n import SUPPORTED_LANGUAGES, normalize_language, resolve_language, translate
from .parsers import parse_source
from .render import render_outputs
from .sync import SyncAlreadyRunning, sync_all


def build_parser(language: str = "en") -> argparse.ArgumentParser:
    """Build the CLI in one of the supported interface languages."""

    language = normalize_language(language)
    t = lambda key, **values: translate(key, language, **values)
    parser = argparse.ArgumentParser(
        prog="aaron-reader",
        description=t("cli.description"),
    )
    parser.add_argument("--config", help=t("cli.help.config"))
    parser.add_argument("--database", help=t("cli.help.database"))
    parser.add_argument("--output", help=t("cli.help.output"))
    parser.add_argument(
        "--language",
        type=_language_value,
        choices=SUPPORTED_LANGUAGES,
        default=None,
        help=t("cli.help.language"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help=t("cli.help.sync"))
    sync_parser.add_argument(
        "--source", action="append", default=[], help=t("cli.help.sync_source")
    )
    sync_parser.add_argument("--force", action="store_true", help=t("cli.help.force"))

    status_parser = subparsers.add_parser("status", help=t("cli.help.status"))
    status_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))
    status_parser.add_argument("--strict", action="store_true", help=t("cli.help.strict"))

    subparsers.add_parser("render", help=t("cli.help.render"))

    crawl_export_parser = subparsers.add_parser(
        "crawl-export", help=t("cli.help.crawl_export")
    )
    crawl_export_parser.add_argument("path", help=t("cli.help.crawl_state_path"))
    crawl_export_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    crawl_import_parser = subparsers.add_parser(
        "crawl-import", help=t("cli.help.crawl_import")
    )
    crawl_import_parser.add_argument("path", help=t("cli.help.crawl_state_path"))
    crawl_import_parser.add_argument(
        "--seed", action="store_true", help=t("cli.help.crawl_seed")
    )
    crawl_import_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    ai_cache_export_parser = subparsers.add_parser(
        "ai-cache-export", help=t("cli.help.ai_cache_export")
    )
    ai_cache_export_parser.add_argument(
        "path", help=t("cli.help.ai_cache_path")
    )
    ai_cache_export_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    ai_cache_import_parser = subparsers.add_parser(
        "ai-cache-import", help=t("cli.help.ai_cache_import")
    )
    ai_cache_import_parser.add_argument(
        "path", help=t("cli.help.ai_cache_path")
    )
    ai_cache_import_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    doctor_parser = subparsers.add_parser("doctor", help=t("cli.help.doctor"))
    doctor_parser.add_argument("--live", action="store_true", help=t("cli.help.live"))

    ai_parser = subparsers.add_parser("ai", help=t("cli.help.ai"))
    ai_subparsers = ai_parser.add_subparsers(dest="ai_command", required=True)

    cloud_run_parser = ai_subparsers.add_parser(
        "cloud-run",
        help="Run a fixed cloud AI enrichment cycle for GitHub Actions",
    )
    cloud_run_parser.add_argument(
        "--provider",
        choices=SUPPORTED_AI_PROVIDERS,
        default=None,
        help="Fixed AI provider profile (defaults to config.ai.provider)",
    )
    cloud_run_parser.add_argument(
        "--fallback-provider",
        choices=("none",) + SUPPORTED_AI_PROVIDERS,
        default=None,
        help=(
            "One-way fallback profile (defaults to config.ai.fallback_provider; "
            "production supports only openrouter -> deepseek)"
        ),
    )
    cloud_run_parser.add_argument(
        "--limit",
        type=lambda value: _article_limit(value, language),
        default=50,
        help="Maximum number of articles with missing AI coverage to process",
    )
    held_retry_group = cloud_run_parser.add_mutually_exclusive_group()
    held_retry_group.add_argument(
        "--force-held",
        action="store_true",
        help=(
            "Explicitly retry stable generations held after an ambiguous or paid "
            "failure; this may create another billable request"
        ),
    )
    held_retry_group.add_argument(
        "--retry-paid-failures",
        action="store_true",
        help=(
            "Retry only generations held after a paid failure; ambiguous results "
            "remain protected from automatic replay"
        ),
    )
    cloud_run_parser.add_argument(
        "--yes",
        action="store_true",
        help=t("cli.help.ai_yes"),
    )
    cloud_run_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    # Accept ``--language`` after the command as well as before it.  Suppressing
    # the subparser default prevents it from overwriting a value parsed by the
    # root parser.
    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "--language",
            type=_language_value,
            choices=SUPPORTED_LANGUAGES,
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
    for subparser in ai_subparsers.choices.values():
        subparser.add_argument(
            "--language",
            type=_language_value,
            choices=SUPPORTED_LANGUAGES,
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
    return parser


def _language_value(value: str) -> str:
    try:
        return normalize_language(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _article_limit(value: str, language: str = "en") -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(translate("cli.error.limit_integer", language)) from exc
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError(translate("cli.error.limit_range", language))
    return limit


def _language_hint(argv: Sequence[str]) -> str:
    """Choose a parser language without loading mutable application state."""

    for index, argument in enumerate(argv):
        if argument == "--language" and index + 1 < len(argv):
            try:
                return normalize_language(argv[index + 1])
            except ValueError:
                return "en"
        if argument.startswith("--language="):
            try:
                return normalize_language(argument.split("=", 1)[1])
            except ValueError:
                return "en"
    try:
        return normalize_language(os.environ.get("AARON_READER_LANG"))
    except ValueError:
        return "en"


def _hold_is_preexisting(exc_detail: str, preexisting_hold_keys: frozenset) -> bool:
    """Check if an AIGenerationHeld exception refers to a pre-existing hold.

    The exception message contains the hold key's first 12 characters in the
    format "(..., <hold_key_prefix>)".  Match against the pre-existing set.
    """

    import re
    match = re.search(r",\s*([a-f0-9]{12})\)?$", exc_detail)
    if not match:
        return False
    prefix = match.group(1)
    return any(key.startswith(prefix) for key in preexisting_hold_keys)


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    language = _language_hint(raw_argv)
    args = build_parser(language).parse_args(raw_argv)
    try:
        config = load_config(args.config)
        language = resolve_language(
            getattr(args, "language", None), config.default_language
        )
        database_path = resolve_project_path(args.database or config.database_path)
        output_dir = resolve_project_path(args.output or config.output_dir)
        database = Database(database_path)
        database.initialize()
        database.sync_source_configs(config.sources, language=language)
        if args.command == "ai":
            return _run_ai_command(
                args,
                config=config,
                database=database,
                output_dir=output_dir,
                language=language,
            )

        if args.command == "sync":
            result = sync_all(
                config,
                database,
                source_slugs=args.source,
                force=args.force,
                language=language,
            )
            render_outputs(database, output_dir, language=language)
            for source in result.sources:
                if source.status == "error":
                    print(
                        translate(
                            "cli.sync.error",
                            language,
                            source=source.source_slug,
                            error=source.error,
                        ),
                        file=sys.stderr,
                    )
                elif source.status in ("not_modified", "unchanged"):
                    print(
                        translate(
                            "cli.sync.no_change",
                            language,
                            source=source.source_slug,
                            status=source.status,
                        )
                    )
                else:
                    print(
                        translate(
                            "cli.sync.summary",
                            language,
                            source=source.source_slug,
                            discovered=source.discovered,
                            inserted=source.inserted,
                            updated=source.updated,
                            seeded=source.seeded,
                            unread=source.unread_new,
                        )
                    )
                if source.warning:
                    print(
                        translate("cli.sync.warning", language, warning=source.warning),
                        file=sys.stderr,
                    )
            print(
                translate("cli.reading_page", language, path=output_dir / "index.html")
            )
            return 1 if result.failed else 0

        if args.command == "crawl-export":
            result = export_crawler_state(
                database,
                config.sources,
                resolve_project_path(args.path),
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    translate(
                        "cli.crawl.exported",
                        language,
                        path=result["path"],
                        sources=result["sources"],
                        articles=result["articles"],
                        seen_urls=result["seen_urls"],
                    )
                )
            return 0

        if args.command == "crawl-import":
            result = import_crawler_state(
                database,
                config.sources,
                resolve_project_path(args.path),
                seed=bool(args.seed),
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    translate(
                        "cli.crawl.imported",
                        language,
                        mode=result["mode"],
                        articles=result["articles"],
                        inserted=result["inserted"],
                        updated=result["updated"],
                    )
                )
            return 0

        if args.command == "ai-cache-export":
            # Keep the deterministic crawler path free of AI orchestration
            # imports.  This module constructs no provider client and makes no
            # model call; it only validates and publishes existing cache rows.
            from .ai_cache import export_ai_cache

            result = export_ai_cache(
                database,
                config,
                resolve_project_path(args.path),
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    translate(
                        "cli.ai_cache.exported",
                        language,
                        path=result["path"],
                        artifacts=result["artifacts"],
                        reports=result["reports"],
                    )
                )
            return 0

        if args.command == "ai-cache-import":
            # Import performs no provider request and never reads an API key.
            from .ai_cache import import_ai_cache

            result = import_ai_cache(
                database,
                config,
                resolve_project_path(args.path),
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    translate(
                        "cli.ai_cache.imported",
                        language,
                        artifacts=result["artifacts"],
                        reports=result["reports"],
                        cache_hits=result["artifact_cache_hits"],
                    )
                )
            return 0

        if args.command == "status":
            statuses = database.source_statuses()
            counts = database.counts()
            if args.json:
                print(
                    json.dumps(
                        {
                            "language": language,
                            "supported_languages": list(SUPPORTED_LANGUAGES),
                            "counts": counts,
                            "sources": statuses,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(
                    translate(
                        "cli.status.summary",
                        language,
                        total=counts["total"],
                        unread=counts["unread"],
                        starred=counts["starred"],
                    )
                )
                for source in statuses:
                    health_code = str(source.get("health") or "never_synced")
                    health = translate("health.%s" % health_code, language)
                    print(
                        translate(
                            "cli.status.line",
                            language,
                            slug=source["slug"],
                            health=health,
                            articles=int(source.get("article_count") or 0),
                            unread=int(source.get("unread_count") or 0),
                            pending=int(source.get("pending_count") or 0),
                            last_success=source.get("last_success_at")
                            or translate("cli.status.never", language),
                        )
                    )
                    if source.get("last_error"):
                        print(
                            translate(
                                "cli.status.error", language, error=source["last_error"]
                            )
                        )
                    if source.get("auxiliary_errors"):
                        print(
                            translate(
                                "cli.status.auxiliary",
                                language,
                                error=source["auxiliary_errors"],
                            )
                        )
                    if source.get("pending_errors"):
                        print(
                            translate(
                                "cli.status.queue",
                                language,
                                error=source["pending_errors"],
                            )
                        )
            unhealthy = [
                source
                for source in statuses
                if source.get("enabled") and source.get("health") != "healthy"
            ]
            return 1 if args.strict and unhealthy else 0

        if args.command == "render":
            render_outputs(database, output_dir, language=language)
            print(translate("cli.rendered", language, path=output_dir))
            return 0

        if args.command == "doctor":
            print(
                translate("cli.doctor.config", language, count=len(config.sources))
            )
            print(translate("cli.doctor.sqlite", language, path=database.path))
            print(translate("cli.doctor.parsers", language))
            if args.live:
                client = HttpClient(
                    config.request_timeout_seconds, config.max_response_bytes
                )
                failures = 0
                for source in config.sources:
                    if not source.enabled:
                        continue
                    try:
                        response = client.fetch(source.fetch_url, attempts=1)
                        articles = parse_source(source, response.body)
                        if not articles:
                            raise ValueError(
                                translate("cli.doctor.no_articles", language)
                            )
                        dated = sum(1 for article in articles if article.published_at)
                        print(
                            translate(
                                "cli.doctor.source",
                                language,
                                source=source.slug,
                                status=response.status,
                                articles=len(articles),
                                dated=dated,
                            )
                        )
                    except Exception as exc:
                        failures += 1
                        print(
                            translate(
                                "cli.sync.error",
                                language,
                                source=source.slug,
                                error=exc,
                            ),
                            file=sys.stderr,
                        )
                return 1 if failures else 0
            return 0

        raise ValueError(
            translate("cli.error.unsupported_command", language, command=args.command)
        )
    except SyncAlreadyRunning as exc:
        print(translate("cli.sync_skipped", language, error=exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(translate("cli.error_prefix", language, error=exc), file=sys.stderr)
        return 2


def _run_ai_command(
    args: argparse.Namespace,
    *,
    config,
    database: Database,
    output_dir: Path,
    language: str,
) -> int:
    """Run the opt-in AI command tree without importing it on normal paths."""

    from .ai_provider import ProviderConfigError
    from .ai_service import (
        AIFallbackEligibleError,
        AIGenerationHeld,
        AIService,
        AIServiceError,
    )

    command = args.ai_command
    fallback_service = None
    fallback_profile = None
    if command == "cloud-run":
        profile = ai_provider_profile(args.provider or config.ai.provider)
        requested_fallback = args.fallback_provider
        if requested_fallback is None:
            fallback_name = (
                config.ai.fallback_provider
                if profile.provider == config.ai.provider
                else ""
            )
        else:
            fallback_name = "" if requested_fallback == "none" else requested_fallback
        if fallback_name and not (
            profile.provider == DEFAULT_AI_PROVIDER
            and fallback_name == DEFAULT_AI_FALLBACK_PROVIDER
        ):
            raise ValueError(
                "automatic AI fallback only supports openrouter -> deepseek"
            )
        if fallback_name:
            fallback_profile = ai_provider_profile(fallback_name)
        ai = replace(
            config.ai,
            enabled=True,
            provider=profile.provider,
            fallback_provider=fallback_name,
            translation_model=profile.model,
            summary_model=profile.model,
            reasoning_effort="none",
            store=False,
            api_key_environment=profile.api_key_environment,
            input_policy="metadata_only",
            max_input_chars_per_article=12_000,
            max_output_tokens_summary=400,
            max_output_tokens_translation=800,
            timeout_seconds=60,
            max_response_bytes=2_000_000,
            summary_enabled=False,
            translation_enabled=True,
            full_text_enabled=False,
            budget=replace(
                config.ai.budget,
                timezone="America/Los_Angeles",
            ),
        )
        config = replace(config, ai=ai)
    service = AIService(
        config,
        database,
        automatic_fallback_provider=(
            fallback_profile.provider if fallback_profile is not None else ""
        ),
    )
    if fallback_profile is not None:
        fallback_ai = replace(
            ai,
            provider=fallback_profile.provider,
            fallback_provider="",
            translation_model=fallback_profile.model,
            summary_model=fallback_profile.model,
            api_key_environment=fallback_profile.api_key_environment,
        )
        fallback_service = AIService(
            replace(config, ai=fallback_ai),
            database,
            allow_fallback_pending_from=profile.provider,
        )
    try:
        if command == "cloud-run":
            from .database import AIBudgetExceeded

            if not args.yes:
                raise ValueError(translate("cli.error.ai_yes_required", language))
            previous_attempts = database.list_ai_attempts(1)
            baseline_attempt_id = (
                int(previous_attempts[0]["id"]) if previous_attempts else 0
            )
            preexisting_hold_keys = frozenset(
                str(hold.get("hold_key") or "")
                for hold in database.list_ai_generation_holds()
            )
            now = datetime.now(timezone.utc).replace(microsecond=0)
            result = {
                "provider": ai.provider,
                "model": ai.translation_model,
                "api_key_environment": ai.api_key_environment,
                "primary_provider": ai.provider,
                "fallback_provider": (
                    fallback_profile.provider
                    if fallback_profile is not None
                    else ""
                ),
                "fallback_enabled": fallback_service is not None,
                "fallback_activated": False,
                "fallback_events": [],
                "active_provider": ai.provider,
                "degraded": False,
                "provider_api_calls_by_provider": {},
                "model_preflight": {
                    "extra_network_call": False,
                    "validation": "fixed chat-completions request",
                },
                "started_at": now.isoformat().replace("+00:00", "Z"),
                "force_held": bool(args.force_held),
                "retry_paid_failures": bool(args.retry_paid_failures),
                "article_limit": int(args.limit),
                "provider_api_calls": 0,
                "coverage_cache_hits": 0,
                "generation_holds_skipped": 0,
                "generation_holds_preexisting_skipped": 0,
                "article_results": [],
                "failures": [],
            }
            active_service = service

            def latest_attempt_id() -> int:
                attempts = database.list_ai_attempts(1)
                return int(attempts[0]["id"]) if attempts else 0

            def execute_provider_operation(operation, workload):
                nonlocal active_service
                before_attempt_id = latest_attempt_id()
                selected = active_service
                try:
                    value = operation(selected, selected is service)
                except AIFallbackEligibleError as exc:
                    if selected is not service or fallback_service is None:
                        raise
                    active_service = fallback_service
                    selected = fallback_service
                    event = {
                        **dict(workload),
                        "from_provider": service.config.provider,
                        "to_provider": fallback_service.config.provider,
                        "reason": exc.reason_code,
                        "primary_call_made": exc.provider_call_made,
                        "detail": str(exc)[:300],
                    }
                    if exc.generation_hold_key:
                        event["generation_hold"] = exc.generation_hold_key[:12]
                    result["fallback_events"].append(event)
                    result["fallback_activated"] = True
                    result["degraded"] = True
                    result["active_provider"] = selected.config.provider
                    # Automatic fallback never receives the broad force-held
                    # override.  It may continue only a narrowly classified
                    # fallback_pending hold created by the OpenRouter policy.
                    value = operation(selected, False)
                calls = sum(
                    int(attempt["id"]) > before_attempt_id
                    for attempt in database.list_ai_attempts(1000)
                )
                return value, selected.config.provider, calls

            budget_exhausted = False
            articles = database.list_articles(limit=1000)
            result["articles_scanned"] = len(articles)
            missing_seen = 0
            for article in articles:
                article_id = int(article["id"])
                translation = service.current_article_artifact(
                    article_id,
                    task_type="translation",
                    target_language="zh-CN",
                )
                if translation is not None:
                    result["coverage_cache_hits"] += 1
                    continue
                if missing_seen >= int(args.limit):
                    continue
                missing_seen += 1
                try:
                    article_result, used_provider, calls = execute_provider_operation(
                        lambda selected, is_primary: selected.generate_article(
                            article_id,
                            task_type="translation",
                            target_language="zh-CN",
                            input_scope="metadata",
                            translated_fields=("title", "publisher_summary"),
                            trigger_kind="cloud-run",
                            force_held=(
                                bool(args.force_held) if is_primary else False
                            ),
                            retry_paid_failure=bool(args.retry_paid_failures),
                        ),
                        {"kind": "article", "article_id": article_id},
                    )
                    result["provider_api_calls"] += calls
                    result["article_results"].append(
                        {
                            "article_id": article_id,
                            "provider": str(
                                article_result.get("provider") or used_provider
                            ),
                            "provider_api_calls": calls,
                            "cache_hit": bool(article_result.get("cache_hit")),
                            "translation_artifact_id": int(article_result["id"]),
                        }
                    )
                except AIGenerationHeld as exc:
                    result["generation_holds_skipped"] += 1
                    exc_detail = str(exc)[:200]
                    is_preexisting = _hold_is_preexisting(
                        exc_detail, preexisting_hold_keys
                    )
                    if is_preexisting:
                        result["generation_holds_preexisting_skipped"] += 1
                    result["article_results"].append(
                        {
                            "article_id": article_id,
                            "provider_api_calls": 0,
                            "cache_hit": False,
                            "skipped": "generation_hold",
                            "preexisting": is_preexisting,
                            "detail": exc_detail,
                        }
                    )
                except AIServiceError as exc:
                    result["failures"].append(
                        {
                            "kind": "article",
                            "article_id": article_id,
                            "error": str(exc)[:500],
                        }
                    )
                except AIBudgetExceeded as exc:
                    result["failures"].append(
                        {
                            "kind": "article",
                            "article_id": article_id,
                            "error": str(exc)[:500],
                        }
                    )
                    budget_exhausted = True
                    break
                except ProviderConfigError as exc:
                    result["failures"].append(
                        {
                            "kind": "article",
                            "article_id": article_id,
                            "error": str(exc)[:500],
                        }
                    )
                    break
            result["articles_missing_considered"] = missing_seen
            result["budget_exhausted"] = budget_exhausted
            # A durable hold prevents an automatic duplicate bill, but it still
            # needs operator attention.  Continue processing unrelated work so
            # safe progress can be published, then leave the workflow visibly
            # failed instead of silently reporting a complete AI cycle.
            #
            # Pre-existing holds (imported from a previous run's ai-cache) that
            # were merely skipped should not cause the job to fail; they require
            # manual attention but do not indicate a regression this cycle.  New
            # holds created THIS cycle (failures or ambiguous results) still fail.
            new_holds_this_cycle = (
                int(result["generation_holds_skipped"])
                - int(result["generation_holds_preexisting_skipped"])
            )
            result["generation_holds_new_this_cycle"] = new_holds_this_cycle
            result["completed"] = (
                not result["failures"]
                and new_holds_this_cycle == 0
            )
            audited_attempts = _ai_attempts_after(
                database,
                baseline_attempt_id,
            )
            provider_counts = {}
            for attempt in audited_attempts:
                requested_provider = str(
                    attempt.get("requested_provider") or "unknown"
                )
                provider_counts[requested_provider] = (
                    int(provider_counts.get(requested_provider, 0)) + 1
                )
            result["provider_api_calls"] = len(audited_attempts)
            result["provider_api_calls_by_provider"] = provider_counts
            result["active_provider"] = active_service.config.provider
            result["usage"] = _ai_usage_after(
                database,
                baseline_attempt_id,
            )
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
            else:
                print(
                    "%s cloud run: %d API call(s), %d cached article(s), "
                    "%d failure(s)"
                    % (
                        ai.provider,
                        result["provider_api_calls"],
                        result["coverage_cache_hits"],
                        len(result["failures"]),
                    )
                )
            return 0 if result["completed"] else 1

        raise ValueError(
            translate("cli.error.unsupported_command", language, command=command)
        )
    except (AIServiceError, ProviderConfigError) as exc:
        raise ValueError(str(exc)) from exc


def _ai_attempts_after(database: Database, baseline_attempt_id: int) -> list:
    return [
        attempt
        for attempt in database.list_ai_attempts(1000)
        if int(attempt["id"]) > int(baseline_attempt_id)
    ]


def _ai_usage_after(database: Database, baseline_attempt_id: int) -> dict:
    attempts = _ai_attempts_after(database, baseline_attempt_id)
    usage = {
        "requests": len(attempts),
        "confirmed_requests": 0,
        "unconfirmed_requests": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_miss_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "reserved_total_tokens_for_unconfirmed": 0,
    }
    for attempt in attempts:
        actual_total = attempt.get("actual_total_tokens")
        if actual_total is None:
            usage["unconfirmed_requests"] += 1
            if int(attempt.get("reservation_active") or 0):
                usage["reserved_total_tokens_for_unconfirmed"] += int(
                    attempt.get("reserved_total_tokens") or 0
                )
            continue
        input_tokens = int(attempt.get("actual_input_tokens") or 0)
        cached = int(attempt.get("actual_cached_input_tokens") or 0)
        cache_write = int(attempt.get("actual_cache_write_tokens") or 0)
        usage["confirmed_requests"] += 1
        usage["input_tokens"] += input_tokens
        usage["cached_input_tokens"] += cached
        usage["cache_write_input_tokens"] += cache_write
        usage["cache_miss_input_tokens"] += max(
            0, input_tokens - cached - cache_write
        )
        usage["output_tokens"] += int(
            attempt.get("actual_output_tokens") or 0
        )
        usage["reasoning_tokens"] += int(
            attempt.get("actual_reasoning_tokens") or 0
        )
        usage["total_tokens"] += int(actual_total)
    return usage
