import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from .config import load_config, resolve_project_path
from .database import Database
from .http_client import HttpClient
from .i18n import SUPPORTED_LANGUAGES, normalize_language, resolve_language, translate
from .parsers import parse_source
from .render import build_llm_packet, render_digest, render_index, render_outputs
from .server import serve, validate_server_options
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
    sync_parser.add_argument(
        "--keep-history-unread",
        action="store_true",
        help=t("cli.help.keep_history_unread"),
    )
    notify_group = sync_parser.add_mutually_exclusive_group()
    notify_group.add_argument(
        "--notify", dest="notify", action="store_true", help=t("cli.help.notify")
    )
    notify_group.add_argument(
        "--no-notify", dest="notify", action="store_false", help=t("cli.help.no_notify")
    )
    sync_parser.set_defaults(notify=True)

    list_parser = subparsers.add_parser("list", help=t("cli.help.list"))
    _add_article_filters(list_parser, language=language)
    list_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    status_parser = subparsers.add_parser("status", help=t("cli.help.status"))
    status_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))
    status_parser.add_argument("--strict", action="store_true", help=t("cli.help.strict"))

    digest_parser = subparsers.add_parser("digest", help=t("cli.help.digest"))
    _add_article_filters(digest_parser, default_unread=True, language=language)

    packet_parser = subparsers.add_parser("packet", help=t("cli.help.packet"))
    _add_article_filters(packet_parser, default_unread=True, language=language)
    packet_parser.add_argument(
        "--max-chars",
        type=lambda value: _character_budget(value, language),
        default=6000,
        help=t("cli.help.max_chars"),
    )

    for command, help_key in (("read", "cli.help.read"), ("unread", "cli.help.unread")):
        action = subparsers.add_parser(command, help=t(help_key))
        action.add_argument("ids", nargs="*", type=int, help=t("cli.help.ids"))
        action.add_argument("--all", action="store_true", help=t("cli.help.all_action"))
        action.add_argument("--source", help=t("cli.help.source_all"))

    for command, help_key in (("star", "cli.help.star"), ("unstar", "cli.help.unstar")):
        action = subparsers.add_parser(command, help=t(help_key))
        action.add_argument("id", type=int, help=t("cli.help.id"))

    subparsers.add_parser("render", help=t("cli.help.render"))

    serve_parser = subparsers.add_parser("serve", help=t("cli.help.serve"))
    serve_parser.add_argument("--host", default="127.0.0.1", help=t("cli.help.host"))
    serve_parser.add_argument(
        "--port",
        type=lambda value: _port_number(value, language),
        default=8765,
        help=t("cli.help.port"),
    )
    serve_parser.add_argument("--open", action="store_true", help=t("cli.help.open"))
    serve_parser.add_argument(
        "--allow-network", action="store_true", help=t("cli.help.allow_network")
    )
    serve_parser.add_argument(
        "--enable-ai-actions",
        action="store_true",
        help=t("cli.help.ai_enable_actions"),
    )

    doctor_parser = subparsers.add_parser("doctor", help=t("cli.help.doctor"))
    doctor_parser.add_argument("--live", action="store_true", help=t("cli.help.live"))

    ai_parser = subparsers.add_parser("ai", help=t("cli.help.ai"))
    ai_subparsers = ai_parser.add_subparsers(dest="ai_command", required=True)

    preview_parser = ai_subparsers.add_parser(
        "preview", help=t("cli.help.ai_preview")
    )
    preview_parser.add_argument("id", type=int, help=t("cli.help.id"))
    preview_parser.add_argument(
        "--task", choices=("summary", "translation"), default="summary",
        help=t("cli.help.ai_task"),
    )
    _add_ai_target(preview_parser, language)
    preview_parser.add_argument(
        "--input-scope", choices=("metadata", "full_text"), default="metadata",
        help=t("cli.help.ai_scope"),
    )
    preview_parser.add_argument(
        "--field", action="append", choices=("title", "publisher-summary"),
        help=t("cli.help.ai_field"),
    )

    summarize_parser = ai_subparsers.add_parser(
        "summarize", help=t("cli.help.ai_summarize")
    )
    summarize_parser.add_argument("id", type=int, help=t("cli.help.id"))
    _add_ai_target(summarize_parser, language)
    summarize_parser.add_argument(
        "--full-text", action="store_true", help=t("cli.help.ai_full_text")
    )
    summarize_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    translate_parser = ai_subparsers.add_parser(
        "translate", help=t("cli.help.ai_translate")
    )
    translate_parser.add_argument("id", type=int, help=t("cli.help.id"))
    _add_ai_target(translate_parser, language)
    translate_parser.add_argument(
        "--field", action="append", choices=("title", "publisher-summary"),
        help=t("cli.help.ai_field"),
    )
    translate_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    ai_digest_parser = ai_subparsers.add_parser(
        "digest", help=t("cli.help.ai_digest")
    )
    _add_article_filters(ai_digest_parser, default_unread=True, language=language)
    _add_ai_target(ai_digest_parser, language)
    ai_digest_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    fetch_parser = ai_subparsers.add_parser("fetch", help=t("cli.help.ai_fetch"))
    fetch_parser.add_argument("id", type=int, help=t("cli.help.id"))
    fetch_parser.add_argument(
        "--refresh", action="store_true", help=t("cli.help.ai_refresh")
    )
    fetch_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    ai_status_parser = ai_subparsers.add_parser(
        "status", help=t("cli.help.ai_status")
    )
    ai_status_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    subscription_export_parser = ai_subparsers.add_parser(
        "subscription-export", help=t("cli.help.ai_subscription_export")
    )
    _add_article_filters(
        subscription_export_parser, default_unread=True, language=language
    )
    subscription_export_parser.set_defaults(limit=10)
    _add_ai_target(subscription_export_parser, language)
    subscription_export_parser.add_argument(
        "--article-id",
        dest="subscription_article_ids",
        action="append",
        type=int,
        help=t("cli.help.ai_subscription_article_id"),
    )
    subscription_export_parser.add_argument(
        "--task",
        dest="subscription_tasks",
        action="append",
        choices=("summary", "translation"),
        help=t("cli.help.ai_subscription_task"),
    )
    subscription_export_parser.add_argument(
        "--output",
        dest="subscription_output",
        help=t("cli.help.ai_subscription_output"),
    )

    subscription_import_parser = ai_subparsers.add_parser(
        "subscription-import", help=t("cli.help.ai_subscription_import")
    )
    subscription_import_parser.add_argument(
        "path", help=t("cli.help.ai_subscription_result")
    )
    subscription_import_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    subscription_report_export_parser = ai_subparsers.add_parser(
        "subscription-report-export",
        help=t("cli.help.ai_subscription_report_export"),
    )
    subscription_report_export_parser.add_argument(
        "--period",
        required=True,
        choices=("daily", "weekly"),
        help=t("cli.help.ai_subscription_report_period"),
    )
    _add_ai_target(subscription_report_export_parser, language)
    subscription_report_export_parser.add_argument(
        "--output",
        dest="subscription_output",
        help=t("cli.help.ai_subscription_output"),
    )

    subscription_report_import_parser = ai_subparsers.add_parser(
        "subscription-report-import",
        help=t("cli.help.ai_subscription_report_import"),
    )
    subscription_report_import_parser.add_argument(
        "path", help=t("cli.help.ai_subscription_report_result")
    )
    subscription_report_import_parser.add_argument(
        "--json", action="store_true", help=t("cli.help.json")
    )

    batch_parser = ai_subparsers.add_parser("batch", help=t("cli.help.ai_batch"))
    _add_article_filters(batch_parser, default_unread=True, language=language)
    _add_ai_target(batch_parser, language)
    batch_parser.add_argument(
        "--task", choices=("summary", "translation"), default="summary",
        help=t("cli.help.ai_task"),
    )
    batch_parser.add_argument(
        "--full-text", action="store_true", help=t("cli.help.ai_full_text")
    )
    batch_parser.add_argument("--yes", action="store_true", help=t("cli.help.ai_yes"))
    batch_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    worker_parser = ai_subparsers.add_parser("worker", help=t("cli.help.ai_worker"))
    worker_parser.add_argument(
        "--limit", type=lambda value: _article_limit(value, language), default=None,
        help=t("cli.help.limit"),
    )
    worker_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    audit_parser = ai_subparsers.add_parser("audit", help=t("cli.help.ai_audit"))
    audit_parser.add_argument(
        "--limit", type=lambda value: _article_limit(value, language), default=100,
        help=t("cli.help.limit"),
    )
    audit_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    retry_parser = ai_subparsers.add_parser("retry", help=t("cli.help.ai_retry"))
    retry_parser.add_argument("job_id", type=int, help="AI job ID")
    retry_parser.add_argument(
        "--allow-unknown", action="store_true", help=t("cli.help.ai_allow_unknown")
    )
    retry_parser.add_argument("--yes", action="store_true", help=t("cli.help.ai_yes"))
    retry_parser.add_argument("--json", action="store_true", help=t("cli.help.json"))

    purge_parser = ai_subparsers.add_parser("purge", help=t("cli.help.ai_purge"))
    purge_parser.add_argument("--before", required=True, help=t("cli.help.ai_before"))
    purge_parser.add_argument(
        "--keep-snapshots", action="store_true", help=t("cli.help.ai_keep_snapshots")
    )
    purge_parser.add_argument("--yes", action="store_true", help=t("cli.help.ai_yes"))

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


def _add_ai_target(parser: argparse.ArgumentParser, language: str) -> None:
    parser.add_argument(
        "--to",
        dest="target_language",
        type=_language_value,
        choices=SUPPORTED_LANGUAGES,
        default="zh-CN",
        help=translate("cli.help.ai_to", language),
    )


def _add_article_filters(
    parser: argparse.ArgumentParser,
    default_unread: bool = False,
    language: str = "en",
) -> None:
    parser.add_argument(
        "--limit",
        type=lambda value: _article_limit(value, language),
        default=100,
        help=translate("cli.help.limit", language),
    )
    parser.add_argument("--source", help=translate("cli.help.source_filter", language))
    parser.add_argument(
        "--query", default="", help=translate("cli.help.query", language)
    )
    parser.add_argument(
        "--unread",
        action="store_true",
        default=default_unread,
        help=translate("cli.help.unread_filter", language),
    )
    parser.add_argument(
        "--all", action="store_true", help=translate("cli.help.all_filter", language)
    )
    parser.add_argument(
        "--starred",
        action="store_true",
        help=translate("cli.help.starred_filter", language),
    )


def _language_value(value: str) -> str:
    try:
        return normalize_language(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _port_number(value: str, language: str = "en") -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(translate("cli.error.port_integer", language)) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(translate("cli.error.port_range", language))
    return port


def _article_limit(value: str, language: str = "en") -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(translate("cli.error.limit_integer", language)) from exc
    if not 1 <= limit <= 1000:
        raise argparse.ArgumentTypeError(translate("cli.error.limit_range", language))
    return limit


def _character_budget(value: str, language: str = "en") -> int:
    try:
        budget = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(translate("cli.error.budget_integer", language)) from exc
    if not 500 <= budget <= 100_000:
        raise argparse.ArgumentTypeError(translate("cli.error.budget_range", language))
    return budget


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    language = _language_hint(raw_argv)
    args = build_parser(language).parse_args(raw_argv)
    try:
        # Reject accidental network exposure before reading configuration or
        # touching the database.
        if args.command == "serve":
            validate_server_options(
                args.host,
                args.port,
                args.allow_network,
                language=language,
                enable_ai_actions=args.enable_ai_actions,
            )
        config = load_config(args.config)
        language = resolve_language(
            getattr(args, "language", None), config.default_language
        )
        database_path = resolve_project_path(args.database or config.database_path)
        output_dir = resolve_project_path(args.output or config.output_dir)
        database = Database(database_path)
        database.initialize()
        database.sync_source_configs(config.sources, language=language)
        known_source_slugs = {source.slug for source in config.sources}
        requested_source = getattr(args, "source", None)
        if (
            (
                args.command in ("list", "digest", "packet", "read", "unread")
                or (
                args.command == "ai"
                    and getattr(args, "ai_command", "")
                    in ("digest", "batch", "subscription-export")
                )
            )
            and isinstance(requested_source, str)
            and requested_source
            and requested_source not in known_source_slugs
        ):
            raise ValueError(
                translate("cli.error.unknown_source", language, source=requested_source)
            )

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
                notify=args.notify,
                force=args.force,
                keep_history_unread=args.keep_history_unread,
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

        if args.command == "list":
            articles = _filtered_articles(database, args)
            if args.json:
                print(json.dumps(articles, ensure_ascii=False, indent=2, default=str))
            else:
                _print_articles(articles, language)
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

        if args.command == "digest":
            print(
                render_digest(_filtered_articles(database, args), language=language),
                end="",
            )
            return 0

        if args.command == "packet":
            packet = build_llm_packet(
                _filtered_articles(database, args),
                max_chars=args.max_chars,
                language=language,
            )
            print(json.dumps(packet, ensure_ascii=False, indent=2))
            return 0

        if args.command in ("read", "unread"):
            if args.all and args.ids:
                raise ValueError(translate("cli.error.ids_with_all", language))
            if args.source and not args.all:
                raise ValueError(translate("cli.error.source_requires_all", language))
            if not args.all and not args.ids:
                raise ValueError(translate("cli.error.ids_or_all", language))
            if args.all:
                changed = (
                    database.mark_all_read(args.source)
                    if args.command == "read"
                    else database.mark_all_unread(args.source)
                )
            else:
                changed = database.set_read(args.ids, read=args.command == "read")
            render_outputs(database, output_dir, language=language)
            print(translate("cli.updated_articles", language, count=changed))
            return 0

        if args.command in ("star", "unstar"):
            changed = database.set_starred(args.id, starred=args.command == "star")
            if not changed:
                raise ValueError(
                    translate(
                        "cli.error.article_not_found", language, article_id=args.id
                    )
                )
            render_outputs(database, output_dir, language=language)
            print(translate("cli.updated_article", language, article_id=args.id))
            return 0

        if args.command == "render":
            render_outputs(database, output_dir, language=language)
            print(translate("cli.rendered", language, path=output_dir))
            return 0

        if args.command == "serve":
            controller = None
            index_renderer = None
            if args.enable_ai_actions:
                if not config.ai.enabled:
                    raise ValueError(
                        "AI web actions require config.ai.enabled=true"
                    )
                if not config.ai.web_actions_enabled:
                    raise ValueError(
                        "AI web actions require config.ai.features.web_actions=true"
                    )
                from .ai_service import AIService, AIWebController

                ai_service = AIService(config, database)
                controller = AIWebController(
                    ai_service,
                    on_complete=lambda: render_outputs(
                        database,
                        output_dir,
                        language=language,
                    ),
                )
                index_renderer = lambda: render_index(
                    database,
                    language=language,
                    ai_actions=True,
                )
            render_outputs(
                database,
                output_dir,
                language=language,
            )
            serve(
                output_dir,
                args.host,
                args.port,
                open_browser=args.open,
                allow_network=args.allow_network,
                language=language,
                enable_ai_actions=args.enable_ai_actions,
                controller=controller,
                index_renderer=index_renderer,
            )
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
    from .ai_service import AIInputError, AIService, AIServiceError

    service = AIService(config, database)
    command = args.ai_command
    try:
        if command == "subscription-export":
            from .ai_subscription import (
                export_subscription_batch,
                suggested_result_path,
                write_subscription_request,
            )

            selected_ids = getattr(args, "subscription_article_ids", None)
            if selected_ids:
                articles = []
                seen_ids = set()
                for article_id in selected_ids:
                    if int(article_id) in seen_ids:
                        continue
                    seen_ids.add(int(article_id))
                    article = database.article(int(article_id))
                    if article is None:
                        raise AIInputError(
                            "article ID %s was not found" % article_id
                        )
                    articles.append(article)
            else:
                articles = database.list_articles(
                    limit=1000,
                    source_slug=getattr(args, "source", None),
                    unread_only=bool(getattr(args, "unread", False))
                    and not bool(getattr(args, "all", False)),
                    starred_only=bool(getattr(args, "starred", False)),
                    query=str(getattr(args, "query", "")),
                )
            request = export_subscription_batch(
                service,
                articles,
                target_language=args.target_language,
                limit=args.limit,
                tasks=getattr(args, "subscription_tasks", None),
            )
            if args.subscription_output:
                request_path = resolve_project_path(args.subscription_output)
                write_subscription_request(request_path, request)
                status = {
                    "protocol": request["protocol"],
                    "batch_id": request["batch_id"],
                    "pending_count": request["pending_count"],
                    "request_path": str(request_path),
                    "suggested_result_path": str(
                        suggested_result_path(request_path)
                    ),
                    "provider_api_calls": 0,
                    "api_key_used": False,
                }
                print(
                    json.dumps(
                        status,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            else:
                print(
                    json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            return 0

        if command == "subscription-report-export":
            from .ai_subscription import (
                export_subscription_report,
                suggested_result_path,
                write_subscription_request,
            )

            request = export_subscription_report(
                service,
                period=args.period,
                target_language=args.target_language,
            )
            if args.subscription_output:
                request_path = resolve_project_path(args.subscription_output)
                write_subscription_request(request_path, request)
                status = {
                    "protocol": request["protocol"],
                    "report_id": request["report_id"],
                    "period": request["period"],
                    "pending_count": request["pending_count"],
                    "article_count": request["article_count"],
                    "cached": request["cached"],
                    "request_path": str(request_path),
                    "suggested_result_path": str(
                        suggested_result_path(request_path)
                    ),
                    "provider_api_calls": 0,
                    "api_key_used": False,
                }
                print(
                    json.dumps(
                        status,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            else:
                print(
                    json.dumps(
                        request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            return 0

        if command == "subscription-report-import":
            from .ai_subscription import (
                import_subscription_report,
                read_subscription_results,
            )

            result_path = resolve_project_path(args.path)
            payload = read_subscription_results(result_path)
            result = import_subscription_report(service, payload)
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    translate(
                        "cli.ai.subscription_report_imported",
                        language,
                        period=result["period"],
                        articles=result["articles"],
                        cache_hits=result["cache_hits"],
                    )
                )
            return 0

        if command == "subscription-import":
            from .ai_subscription import (
                import_subscription_results,
                read_subscription_results,
            )

            result_path = resolve_project_path(args.path)
            payload = read_subscription_results(result_path)
            result = import_subscription_results(service, payload)
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    translate(
                        "cli.ai.subscription_imported",
                        language,
                        articles=result["articles"],
                        artifacts=result["imported_artifacts"],
                        cache_hits=result["cache_hits"],
                    )
                )
            return 0

        if command == "preview":
            fields = args.field or ["title", "publisher-summary"]
            prepared = service.prepare_article(
                args.id,
                task_type=args.task,
                target_language=args.target_language,
                input_scope=args.input_scope,
                translated_fields=fields,
                fetch_if_missing=False,
            )
            print(json.dumps(service.preview(prepared), ensure_ascii=False, indent=2))
            return 0

        if command in ("summarize", "translate"):
            task = "summary" if command == "summarize" else "translation"
            scope = "full_text" if getattr(args, "full_text", False) else "metadata"
            fields = getattr(args, "field", None) or ["title", "publisher-summary"]
            result = service.generate_article(
                args.id,
                task_type=task,
                target_language=args.target_language,
                input_scope=scope,
                translated_fields=fields,
            )
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    translate(
                        "cli.ai.result",
                        language,
                        task=task,
                        scope=scope,
                        language=args.target_language,
                        cache_hit=result.get("cache_hit"),
                    )
                )
                print(str(result.get("output_text") or ""))
            return 0

        if command == "digest":
            articles = _filtered_articles(database, args)
            result = service.generate_digest(
                articles, target_language=args.target_language
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    translate(
                        "cli.ai.result",
                        language,
                        task="digest",
                        scope="digest",
                        language=args.target_language,
                        cache_hit=result.get("cache_hit"),
                    )
                )
                print(str(result.get("output_text") or ""))
            return 0

        if command == "fetch":
            snapshot = service.fetch_content(args.id, refresh=args.refresh)
            if args.json:
                public_snapshot = dict(snapshot)
                public_snapshot.pop("normalized_text", None)
                print(
                    json.dumps(
                        public_snapshot, ensure_ascii=False, indent=2, default=str
                    )
                )
            else:
                print(
                    translate(
                        "cli.ai.fetch_result",
                        language,
                        snapshot_id=snapshot.get("id") or "ephemeral",
                        characters=snapshot.get("character_count") or 0,
                        cache_hit=snapshot.get("cache_hit"),
                    )
                )
            return 0

        if command == "status":
            status = service.status()
            if args.json:
                print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    translate(
                        "cli.ai.status",
                        language,
                        enabled=status["enabled"],
                        api_key_present=status["api_key_present"],
                        artifacts=status["artifacts"],
                        attempts=status["attempts"],
                    )
                )
                print(
                    translate(
                        "cli.ai.status_usage",
                        language,
                        daily_requests=status["daily"]["requests"],
                        daily_tokens=status["daily"]["total_tokens"],
                        monthly_requests=status["monthly"]["requests"],
                        monthly_tokens=status["monthly"]["total_tokens"],
                    )
                )
                for state, count in sorted(status["jobs"].items()):
                    print("  %-18s %s" % (state, count))
            return 0

        if command == "batch":
            if not args.yes:
                raise ValueError(translate("cli.error.ai_yes_required", language))
            articles = _filtered_articles(database, args)
            jobs = service.enqueue_batch(
                articles,
                task_type=args.task,
                target_language=args.target_language,
                input_scope="full_text" if args.full_text else "metadata",
                confirmed=True,
            )
            if args.json:
                print(json.dumps(jobs, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    translate("cli.ai.batch_enqueued", language, count=len(jobs))
                )
            return 0

        if command == "worker":
            results = service.run_worker(limit=args.limit)
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
            else:
                print(translate("cli.ai.worker_result", language, count=len(results)))
                for result in results:
                    print("  %s  %s" % (result["job_id"], result["state"]))
            return 1 if any(result.get("state") != "succeeded" for result in results) else 0

        if command == "audit":
            attempts = service.audit(args.limit)
            if args.json:
                print(json.dumps(attempts, ensure_ascii=False, indent=2, default=str))
            else:
                for attempt in attempts:
                    print(
                        "{id:<5} {state:<10} {task:<12} article={article} "
                        "tokens={tokens} HTTP={http}".format(
                            id=attempt["id"],
                            state=attempt["state"],
                            task=attempt["task_type"],
                            article=attempt.get("article_id") or "-",
                            tokens=(
                                attempt.get("actual_total_tokens")
                                if attempt.get("actual_total_tokens") is not None
                                else attempt.get("reserved_total_tokens")
                            ),
                            http=attempt.get("http_status") or "-",
                        )
                    )
            return 0

        if command == "retry":
            if not args.yes:
                raise ValueError(translate("cli.error.ai_yes_required", language))
            result = service.retry_job(
                args.job_id, allow_unknown=args.allow_unknown
            )
            render_outputs(database, output_dir, language=language)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(
                    translate(
                        "cli.ai.result",
                        language,
                        task=result.get("task_type"),
                        scope=result.get("input_scope"),
                        language=result.get("target_language"),
                        cache_hit=result.get("cache_hit"),
                    )
                )
                print(str(result.get("output_text") or ""))
            return 0

        if command == "purge":
            if not args.yes:
                raise ValueError(translate("cli.error.ai_yes_required", language))
            before = _parse_ai_before(args.before)
            deleted = database.purge_ai_data(
                before, include_snapshots=not args.keep_snapshots
            )
            render_outputs(database, output_dir, language=language)
            print(
                translate(
                    "cli.ai.purged",
                    language,
                    artifacts=deleted["artifacts"],
                    snapshots=deleted["content_snapshots"],
                )
            )
            return 0

        raise ValueError(
            translate("cli.error.unsupported_command", language, command=command)
        )
    except (AIServiceError, ProviderConfigError) as exc:
        raise ValueError(str(exc)) from exc


def _parse_ai_before(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("--before must be an ISO date or timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--before must be an ISO date or timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _filtered_articles(database: Database, args: argparse.Namespace):
    unread_only = bool(getattr(args, "unread", False)) and not bool(
        getattr(args, "all", False)
    )
    return database.list_articles(
        limit=max(1, min(int(args.limit), 1000)),
        source_slug=getattr(args, "source", None),
        unread_only=unread_only,
        starred_only=bool(getattr(args, "starred", False)),
        query=str(getattr(args, "query", "")),
    )


def _print_articles(articles, language: str = "en") -> None:
    if not articles:
        print(translate("cli.no_articles", language))
        return
    for article in articles:
        marker = "●" if not article.get("read_at") else " "
        star = "★" if article.get("starred_at") else " "
        date = str(
            article.get("published_at") or article.get("discovered_at") or ""
        )[:10]
        print(
            "%s%s %-10s [%s] #%s %s"
            % (
                marker,
                star,
                date,
                article["source_slug"],
                article["id"],
                article["title"],
            )
        )
        print("   %s" % article["canonical_url"])
        if article.get("summary"):
            print("   %s" % article["summary"])
