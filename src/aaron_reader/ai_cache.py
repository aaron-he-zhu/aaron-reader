"""Strict public handoff for reusable AI article caches.

The handoff is intentionally portable across ephemeral SQLite databases.  It
never publishes local integer IDs or database cache keys; article identities
are the configured source slug, publisher identity, canonical URL, and current
content hash.  Import resolves those identities again and re-keys each result
for the target database.

Only successful metadata summaries/translations are eligible.  Provider/model
fields are retained as bounded provenance, but they are not a compatibility
gate: a structurally valid historical result can be reused after a provider or
model migration when its article content, prompt, schema, task, scope, and
target language are still compatible.

The format deliberately excludes API keys, provider request identifiers,
jobs, attempts, errors, personal read/star state, notifications, raw inputs,
publisher full text, and content snapshots.  It carries only aggregate usage
totals and input-free generation fingerprints needed to enforce budgets and
prevent accidental paid replays across ephemeral runners.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .ai_prompts import (
    _text_appears_untranslated,
    canonical_json,
    parse_and_validate_output,
    stable_hash,
)
from .ai_service import AIService, PreparedTask
from .crawler_state import _source_url_is_allowed
from .database import Database, utc_now
from .i18n import normalize_language
from .models import AppConfig, SourceConfig


AI_CACHE_PROTOCOL = "aaron-reader-public-ai-cache-v3"
AI_CACHE_MAX_BYTES = 25 * 1024 * 1024
AI_CACHE_MAX_ARTIFACTS = 20_000
AI_CACHE_MAX_USAGE_DAYS = 62
AI_CACHE_MAX_GENERATION_HOLDS = 20_000
AI_CACHE_USAGE_TIMEZONE = "America/Los_Angeles"

_HEX = set("0123456789abcdef")
_TOP_LEVEL_KEYS = {
    "protocol",
    "exported_at",
    "bundle_hash",
    "artifacts",
    "usage_ledger",
    "generation_holds",
}
_IDENTITY_KEYS = {
    "source_slug",
    "external_id",
    "canonical_url",
    "content_hash",
}
_ARTICLE_ARTIFACT_KEYS = {
    "cache_key",
    "article",
    "task_type",
    "input_scope",
    "source_language",
    "target_language",
    "prompt_version",
    "prompt_hash",
    "response_schema_version",
    "response_schema_hash",
    "provider",
    "requested_model",
    "resolved_model",
    "generation_params_hash",
    "output",
    "output_hash",
    "input_truncated",
    "created_at",
}
_USAGE_LEDGER_KEYS = {
    "ledger_key",
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
_USAGE_INTEGER_FIELDS = (
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
_GENERATION_HOLD_KEYS = {
    "hold_key",
    "workload_kind",
    "hold_class",
    "descriptor",
    "first_seen_at",
    "last_seen_at",
}
_GENERATION_HOLD_DESCRIPTOR_KEYS = {
    "protocol",
    "workload_kind",
    "provider",
    "model",
    "task_type",
    "input_scope",
    "target_language",
    "article_identities",
    "portable_input_hash",
    "prompt_version",
    "prompt_hash",
    "schema_name",
    "schema_version",
    "schema_hash",
    "generation_params_hash",
    "max_output_tokens",
}
_GENERATION_HOLD_PROTOCOL = "aaron-reader-ai-generation-hold/v1"

_DB_ARTIFACT_COLUMNS = (
    "article_id",
    "task_type",
    "input_scope",
    "source_language",
    "target_language",
    "artifact_key",
    "input_hash",
    "article_content_hash",
    "source_artifact_id",
    "content_snapshot_id",
    "prompt_version",
    "prompt_hash",
    "response_schema_version",
    "response_schema_hash",
    "provider",
    "requested_model",
    "resolved_model",
    "generation_params_hash",
    "provider_response_id",
    "output_json",
    "output_text",
    "output_hash",
    "status",
    "input_truncated",
    "created_at",
)
_DB_ARTIFACT_IMMUTABLE_COLUMNS = _DB_ARTIFACT_COLUMNS[:-1]


def _cache_hash(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("bundle_hash", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _entry_hash(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("cache_key", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _ledger_hash(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("ledger_key", None)
    return hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(
    pairs: Iterable[Tuple[str, object]],
) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("AI cache contains a duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError("AI cache contains a non-finite JSON number: %s" % value)


def _read_payload(path: Path) -> object:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError("AI cache is not a regular file: %s" % source)
    with source.open("rb") as handle:
        raw = handle.read(AI_CACHE_MAX_BYTES + 1)
    if not raw or len(raw) > AI_CACHE_MAX_BYTES:
        raise ValueError("AI cache size is outside the safe range")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("AI cache must be UTF-8 JSON") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("AI cache is not strict JSON: %s" % exc) from exc


def _atomic_write(path: Path, payload: Mapping[str, object]) -> Path:
    destination = Path(path)
    if destination.is_symlink():
        raise ValueError("AI cache destination cannot be a symbolic link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    if not encoded or len(encoded) > AI_CACHE_MAX_BYTES:
        raise ValueError("AI cache exceeds the publication size limit")

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % destination.name,
            suffix=".tmp",
            dir=str(destination.parent),
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, str(destination))
        temporary_name = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return destination


def _exact_keys(value: object, expected: set, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("AI cache %s fields do not match the contract" % label)
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError("AI cache %s must be a string" % label)
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("AI cache %s must use canonical Unicode" % label)
    if any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("AI cache %s contains a control character" % label)
    if (not value and not allow_empty) or len(value) > maximum:
        raise ValueError("AI cache %s length is outside the safe range" % label)
    return value


def _sha(value: object, label: str) -> str:
    result = _text(value, label, maximum=64)
    if len(result) != 64 or any(character not in _HEX for character in result):
        raise ValueError("AI cache %s must be a SHA-256 hex string" % label)
    return result


def _utc_timestamp(value: object, label: str) -> Tuple[str, datetime]:
    result = _text(value, label, maximum=32)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("AI cache %s must be a UTC timestamp" % label) from exc
    if parsed.tzinfo is None:
        raise ValueError("AI cache %s must be a UTC timestamp" % label)
    canonical = (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if result != canonical:
        raise ValueError("AI cache %s must use canonical UTC seconds" % label)
    return result, parsed.astimezone(timezone.utc)


def _language(value: object, label: str) -> str:
    result = _text(value, label, maximum=32)
    try:
        normalized = normalize_language(result)
    except ValueError as exc:
        raise ValueError("AI cache %s is unsupported" % label) from exc
    if normalized != result:
        raise ValueError("AI cache %s is not normalized" % label)
    return result


def _nonnegative_integer(
    value: object,
    label: str,
    *,
    maximum: int = 1_000_000_000_000_000,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("AI cache %s must be an integer" % label)
    if value < 0 or value > maximum:
        raise ValueError("AI cache %s is outside the safe range" % label)
    return value


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _usage_day_window(moment: datetime) -> Tuple[str, str]:
    zone = ZoneInfo(AI_CACHE_USAGE_TIMEZONE)
    local_day = moment.astimezone(zone).date()
    start = datetime.combine(local_day, time.min, tzinfo=zone)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=zone)
    return _utc_iso(start), _utc_iso(end)


def _validate_usage_entry(
    value: object,
    *,
    index: int,
    exported_datetime: datetime,
) -> Mapping[str, object]:
    label = "usage_ledger[%d]" % index
    entry = _exact_keys(value, _USAGE_LEDGER_KEYS, label)
    if entry["timezone"] != AI_CACHE_USAGE_TIMEZONE:
        raise ValueError(
            "AI cache usage ledger timezone must be %s"
            % AI_CACHE_USAGE_TIMEZONE
        )
    day_start, start = _utc_timestamp(
        entry["day_start"], "%s.day_start" % label
    )
    day_end, end = _utc_timestamp(entry["day_end"], "%s.day_end" % label)
    covered_through, covered = _utc_timestamp(
        entry["covered_through"], "%s.covered_through" % label
    )
    expected_start, expected_end = _usage_day_window(start)
    if day_start != expected_start or day_end != expected_end:
        raise ValueError(
            "AI cache usage ledger boundaries are not one San Francisco day"
        )
    if not start < covered or covered > exported_datetime:
        raise ValueError("AI cache usage ledger covered_through is invalid")
    exported_local_day = exported_datetime.astimezone(
        ZoneInfo(AI_CACHE_USAGE_TIMEZONE)
    ).date()
    entry_local_day = start.astimezone(ZoneInfo(AI_CACHE_USAGE_TIMEZONE)).date()
    if not exported_local_day - timedelta(days=AI_CACHE_MAX_USAGE_DAYS - 1) <= entry_local_day <= exported_local_day:
        raise ValueError("AI cache usage ledger day is outside the retained window")

    totals = {
        field: _nonnegative_integer(
            entry[field],
            "%s.%s" % (label, field),
            maximum=1_000_000_000 if field.endswith("requests") else 1_000_000_000_000,
        )
        for field in _USAGE_INTEGER_FIELDS
    }
    if totals["requests"] <= 0:
        raise ValueError("AI cache usage ledger must omit empty days")
    if (
        totals["confirmed_requests"] + totals["unconfirmed_requests"]
        != totals["requests"]
    ):
        raise ValueError("AI cache usage request subtotals exceed requests")
    if (
        totals["cached_input_tokens"]
        + totals["cache_miss_input_tokens"]
        + totals["cache_write_input_tokens"]
        != totals["input_tokens"]
    ):
        raise ValueError("AI cache usage input token subtotals are inconsistent")
    if totals["reasoning_tokens"] > totals["output_tokens"]:
        raise ValueError("AI cache usage reasoning tokens exceed output tokens")
    if totals["total_tokens"] < totals["input_tokens"] + totals["output_tokens"]:
        raise ValueError("AI cache usage total tokens are inconsistent")
    reserved_tokens = totals["reserved_total_tokens_for_unconfirmed"]
    if reserved_tokens and not totals["unconfirmed_requests"]:
        raise ValueError("AI cache usage unconfirmed reservation totals are inconsistent")
    ledger_key = _sha(entry["ledger_key"], "%s.ledger_key" % label)
    if ledger_key != _ledger_hash(entry):
        raise ValueError("AI cache usage ledger_key does not match its contents")
    del covered_through
    return entry


def _identity_key(identity: Mapping[str, object]) -> Tuple[str, str, str, str]:
    return (
        str(identity["source_slug"]),
        str(identity["external_id"]),
        str(identity["canonical_url"]),
        str(identity["content_hash"]),
    )


def _identity_from_row(row: Mapping[str, object]) -> Dict[str, str]:
    return {
        "source_slug": str(row.get("source_slug") or ""),
        "external_id": str(row.get("external_id") or ""),
        "canonical_url": str(row.get("canonical_url") or ""),
        "content_hash": str(row.get("content_hash") or ""),
    }


def _validate_identity(
    value: object,
    configured_by_slug: Mapping[str, SourceConfig],
    label: str,
) -> Mapping[str, object]:
    identity = _exact_keys(value, _IDENTITY_KEYS, label)
    slug = _text(identity["source_slug"], "%s.source_slug" % label, maximum=100)
    external_id = _text(
        identity["external_id"], "%s.external_id" % label, maximum=4_096
    )
    canonical_url = _text(
        identity["canonical_url"], "%s.canonical_url" % label, maximum=4_096
    )
    try:
        parsed_url = urlsplit(canonical_url)
    except ValueError as exc:
        raise ValueError("AI cache article URL is invalid") from exc
    if (
        parsed_url.scheme.lower() not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.fragment
    ):
        raise ValueError("AI cache article URL contains forbidden components")
    _sha(identity["content_hash"], "%s.content_hash" % label)
    configured = configured_by_slug.get(slug)
    if configured is None:
        raise ValueError("AI cache identity uses an unconfigured source: %s" % slug)
    if not _source_url_is_allowed(configured, canonical_url):
        raise ValueError("AI cache article URL is outside its configured source")
    # Explicit assignments keep static analyzers aware that every field was
    # validated even though the original mapping is returned unchanged.
    del external_id
    return identity


def _validate_generation_hold_entry(
    value: object,
    *,
    index: int,
    configured_by_slug: Mapping[str, SourceConfig],
    exported_datetime: datetime,
) -> Mapping[str, object]:
    """Validate one identifier-free, input-free replay-prevention record."""

    label = "generation_holds[%d]" % index
    entry = _exact_keys(value, _GENERATION_HOLD_KEYS, label)
    workload_kind = entry["workload_kind"]
    if workload_kind not in {"article", "article_pair"}:
        raise ValueError("AI cache generation hold workload_kind is invalid")
    if entry["hold_class"] not in {
        "ambiguous",
        "paid_failure",
        "fallback_pending",
    }:
        raise ValueError("AI cache generation hold class is invalid")

    descriptor = _exact_keys(
        entry["descriptor"],
        _GENERATION_HOLD_DESCRIPTOR_KEYS,
        "%s.descriptor" % label,
    )
    if descriptor["protocol"] != _GENERATION_HOLD_PROTOCOL:
        raise ValueError("AI cache generation hold protocol is unsupported")
    if descriptor["workload_kind"] != workload_kind:
        raise ValueError("AI cache generation hold descriptor workload differs")

    _text(descriptor["provider"], "%s.descriptor.provider" % label, maximum=100)
    _text(descriptor["model"], "%s.descriptor.model" % label, maximum=200)
    task_type = descriptor["task_type"]
    input_scope = descriptor["input_scope"]
    if workload_kind == "article":
        if task_type not in {"summary", "translation"} or input_scope != "metadata":
            raise ValueError("AI cache article generation hold contract is invalid")
    elif workload_kind == "article_pair":
        if task_type != "summary" or input_scope != "metadata":
            raise ValueError("AI cache article-pair generation hold contract is invalid")
    _language(
        descriptor["target_language"],
        "%s.descriptor.target_language" % label,
    )

    identities = descriptor["article_identities"]
    if not isinstance(identities, list) or len(identities) != 1:
        raise ValueError(
            "AI cache generation hold article identity count is outside the safe range"
        )
    validated_identities = [
        _validate_identity(
            identity,
            configured_by_slug,
            "%s.descriptor.article_identities[%d]" % (label, identity_index),
        )
        for identity_index, identity in enumerate(identities)
    ]
    identity_keys = [_identity_key(identity) for identity in validated_identities]
    if len(identity_keys) != len(set(identity_keys)):
        raise ValueError("AI cache generation hold contains duplicate article identities")

    _sha(
        descriptor["portable_input_hash"],
        "%s.descriptor.portable_input_hash" % label,
    )
    _text(
        descriptor["prompt_version"],
        "%s.descriptor.prompt_version" % label,
        maximum=100,
    )
    _sha(descriptor["prompt_hash"], "%s.descriptor.prompt_hash" % label)
    _text(
        descriptor["schema_name"],
        "%s.descriptor.schema_name" % label,
        maximum=100,
    )
    _text(
        descriptor["schema_version"],
        "%s.descriptor.schema_version" % label,
        maximum=100,
    )
    _sha(descriptor["schema_hash"], "%s.descriptor.schema_hash" % label)
    _sha(
        descriptor["generation_params_hash"],
        "%s.descriptor.generation_params_hash" % label,
    )
    max_output_tokens = _nonnegative_integer(
        descriptor["max_output_tokens"],
        "%s.descriptor.max_output_tokens" % label,
        maximum=1_000_000,
    )
    if max_output_tokens <= 0:
        raise ValueError("AI cache generation hold max_output_tokens must be positive")

    _, first_seen = _utc_timestamp(
        entry["first_seen_at"], "%s.first_seen_at" % label
    )
    _, last_seen = _utc_timestamp(
        entry["last_seen_at"], "%s.last_seen_at" % label
    )
    if first_seen > last_seen or last_seen > exported_datetime:
        raise ValueError("AI cache generation hold timestamps are inconsistent")

    # Keep the database's canonical descriptor hashing contract as the final
    # compatibility check.  This also prevents a rehashed outer bundle from
    # changing a hold fingerprint or smuggling a non-finite JSON value.
    try:
        validated = Database._validated_ai_generation_hold(entry)
    except ValueError as exc:
        raise ValueError("AI cache %s is invalid: %s" % (label, exc)) from exc
    hold_key = _sha(entry["hold_key"], "%s.hold_key" % label)
    if hold_key != validated["hold_key"]:
        raise ValueError("AI cache generation hold key does not match descriptor")
    return entry


def _validate_provenance_fields(value: Mapping[str, object], label: str) -> None:
    source_language = _text(
        value["source_language"],
        "%s.source_language" % label,
        maximum=32,
    )
    if source_language != "unknown":
        _language(source_language, "%s.source_language" % label)
    _language(value["target_language"], "%s.target_language" % label)
    _text(value["prompt_version"], "%s.prompt_version" % label, maximum=100)
    _sha(value["prompt_hash"], "%s.prompt_hash" % label)
    _text(
        value["response_schema_version"],
        "%s.response_schema_version" % label,
        maximum=100,
    )
    _sha(value["response_schema_hash"], "%s.response_schema_hash" % label)
    _text(value["provider"], "%s.provider" % label, maximum=100)
    _text(value["requested_model"], "%s.requested_model" % label, maximum=200)
    _text(value["resolved_model"], "%s.resolved_model" % label, maximum=200)
    _sha(
        value["generation_params_hash"],
        "%s.generation_params_hash" % label,
    )
    if not isinstance(value["input_truncated"], bool):
        raise ValueError("AI cache %s.input_truncated must be a JSON boolean" % label)
    _utc_timestamp(value["created_at"], "%s.created_at" % label)


def _validate_article_output(
    entry: Mapping[str, object], label: str
) -> Tuple[Dict[str, object], str]:
    task_type = str(entry["task_type"])
    output = entry["output"]
    if not isinstance(output, dict):
        raise ValueError("AI cache %s.output must be an object" % label)
    try:
        validated, readable = parse_and_validate_output(
            task_type,
            canonical_json(output),
            target_language=str(entry["target_language"]),
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
    except ValueError as exc:
        raise ValueError("AI cache %s.output failed validation: %s" % (label, exc)) from exc
    if canonical_json(validated) != canonical_json(output):
        raise ValueError("AI cache %s.output is not canonical" % label)
    expected_output_hash = stable_hash(canonical_json(validated))
    if _sha(entry["output_hash"], "%s.output_hash" % label) != expected_output_hash:
        raise ValueError("AI cache %s.output_hash does not match output" % label)
    return validated, readable


def _validate_payload(
    payload: object,
    configured_sources: Sequence[SourceConfig],
    *,
    verify_hash: bool,
) -> Mapping[str, object]:
    root = _exact_keys(payload, _TOP_LEVEL_KEYS, "top-level")
    if root["protocol"] != AI_CACHE_PROTOCOL:
        raise ValueError("unsupported AI cache protocol")
    exported_at, exported_datetime = _utc_timestamp(
        root["exported_at"], "exported_at"
    )
    del exported_at
    bundle_hash = _sha(root["bundle_hash"], "bundle_hash")
    if verify_hash and bundle_hash != _cache_hash(root):
        raise ValueError("AI cache bundle hash does not match its contents")

    artifacts = root["artifacts"]
    usage_ledger = root["usage_ledger"]
    generation_holds = root["generation_holds"]
    if not isinstance(artifacts, list) or len(artifacts) > AI_CACHE_MAX_ARTIFACTS:
        raise ValueError("AI cache artifacts exceed the safe cardinality")
    if (
        not isinstance(usage_ledger, list)
        or len(usage_ledger) > AI_CACHE_MAX_USAGE_DAYS
    ):
        raise ValueError("AI cache usage ledger exceeds the safe cardinality")
    if (
        not isinstance(generation_holds, list)
        or len(generation_holds) > AI_CACHE_MAX_GENERATION_HOLDS
    ):
        raise ValueError("AI cache generation holds exceed the safe cardinality")

    configured_by_slug = {source.slug: source for source in configured_sources}
    if not configured_by_slug:
        raise ValueError("AI cache requires at least one configured source")

    artifact_keys = set()
    artifact_bindings = set()
    artifact_order: List[Tuple[object, ...]] = []
    for index, value in enumerate(artifacts):
        label = "artifacts[%d]" % index
        entry = _exact_keys(value, _ARTICLE_ARTIFACT_KEYS, label)
        identity = _validate_identity(entry["article"], configured_by_slug, "%s.article" % label)
        task_type = entry["task_type"]
        if task_type not in {"summary", "translation"}:
            raise ValueError("AI cache article task must be summary or translation")
        if entry["input_scope"] != "metadata":
            raise ValueError("AI cache article artifacts must use metadata input")
        _validate_provenance_fields(entry, label)
        _, created = _utc_timestamp(entry["created_at"], "%s.created_at" % label)
        if created > exported_datetime:
            raise ValueError("AI cache artifact was created after the bundle export")
        _validate_article_output(entry, label)
        cache_key = _sha(entry["cache_key"], "%s.cache_key" % label)
        if cache_key != _entry_hash(entry):
            raise ValueError("AI cache artifact cache_key does not match its contents")
        if cache_key in artifact_keys:
            raise ValueError("AI cache contains a duplicate artifact cache_key")
        artifact_keys.add(cache_key)
        binding = (
            _identity_key(identity),
            str(task_type),
            str(entry["target_language"]),
        )
        if binding in artifact_bindings:
            raise ValueError("AI cache contains duplicate article task coverage")
        artifact_bindings.add(binding)
        artifact_order.append(binding + (cache_key,))
    if artifact_order != sorted(artifact_order):
        raise ValueError("AI cache artifacts are not in canonical order")

    usage_order: List[str] = []
    usage_keys = set()
    for index, value in enumerate(usage_ledger):
        entry = _validate_usage_entry(
            value,
            index=index,
            exported_datetime=exported_datetime,
        )
        ledger_key = str(entry["ledger_key"])
        if ledger_key in usage_keys:
            raise ValueError("AI cache contains a duplicate usage ledger_key")
        usage_keys.add(ledger_key)
        day_start = str(entry["day_start"])
        if day_start in usage_order:
            raise ValueError("AI cache contains duplicate usage day coverage")
        usage_order.append(day_start)
    if usage_order != sorted(usage_order):
        raise ValueError("AI cache usage ledger is not in canonical order")

    hold_order: List[str] = []
    hold_keys = set()
    for index, value in enumerate(generation_holds):
        entry = _validate_generation_hold_entry(
            value,
            index=index,
            configured_by_slug=configured_by_slug,
            exported_datetime=exported_datetime,
        )
        hold_key = str(entry["hold_key"])
        if hold_key in hold_keys:
            raise ValueError("AI cache contains a duplicate generation hold key")
        hold_keys.add(hold_key)
        hold_order.append(hold_key)
    if hold_order != sorted(hold_order):
        raise ValueError("AI cache generation holds are not in canonical order")
    return root


def _prepared_contract_matches(
    prepared: PreparedTask, value: Mapping[str, object]
) -> bool:
    """Return whether a portable artifact is semantically reusable now.

    Provider, model, generation settings, and historical truncation are
    deliberately provenance-only.  Prompt and response-schema hashes remain
    compatibility gates, along with task/scope/language and article content.
    """

    return (
        str(value["task_type"]) == prepared.task_type
        and str(value["input_scope"]) == prepared.input_scope
        and str(value["target_language"]) == prepared.target_language
        and str(value["prompt_version"]) == prepared.definition.prompt_version
        and str(value["prompt_hash"]) == prepared.definition.prompt_hash
        and str(value["response_schema_version"])
        == prepared.definition.schema_version
        and str(value["response_schema_hash"]) == prepared.definition.schema_hash
    )


def _row_contract_matches_prepared(
    row: Mapping[str, object], prepared: PreparedTask
) -> bool:
    return (
        str(row.get("task_type") or "") == prepared.task_type
        and str(row.get("input_scope") or "") == prepared.input_scope
        and str(row.get("target_language") or "") == prepared.target_language
        and str(row.get("input_hash") or "") == prepared.input_hash
        and str(row.get("article_content_hash") or "")
        == prepared.article_content_hash
        and str(row.get("prompt_version") or "")
        == prepared.definition.prompt_version
        and str(row.get("prompt_hash") or "") == prepared.definition.prompt_hash
        and str(row.get("response_schema_version") or "")
        == prepared.definition.schema_version
        and str(row.get("response_schema_hash") or "")
        == prepared.definition.schema_hash
    )


def _portable_artifact_fields(
    row: Mapping[str, object], output: Mapping[str, object]
) -> Dict[str, object]:
    return {
        "task_type": str(row["task_type"]),
        "input_scope": str(row["input_scope"]),
        "source_language": str(row["source_language"]),
        "target_language": str(row["target_language"]),
        "prompt_version": str(row["prompt_version"]),
        "prompt_hash": str(row["prompt_hash"]),
        "response_schema_version": str(row["response_schema_version"]),
        "response_schema_hash": str(row["response_schema_hash"]),
        "provider": str(row["provider"]),
        "requested_model": str(row["requested_model"]),
        "resolved_model": str(row["resolved_model"]),
        "generation_params_hash": str(row["generation_params_hash"]),
        "output": dict(output),
        "output_hash": stable_hash(canonical_json(output)),
        "input_truncated": bool(row["input_truncated"]),
        "created_at": str(row["created_at"]),
    }


def _validated_db_output(
    row: Mapping[str, object], prepared: PreparedTask
) -> Tuple[Dict[str, object], str]:
    try:
        validated, readable = parse_and_validate_output(
            prepared.task_type,
            str(row.get("output_json") or ""),
            target_language=prepared.target_language,
            input_scope=prepared.input_scope,
            translated_fields=prepared.translated_fields,
        )
    except ValueError as exc:
        raise ValueError("stored AI artifact output failed validation: %s" % exc) from exc
    output_json = canonical_json(validated)
    if str(row.get("output_json") or "") != output_json:
        raise ValueError("stored AI artifact output is not canonical JSON")
    if str(row.get("output_hash") or "") != stable_hash(output_json):
        raise ValueError("stored AI artifact output hash is invalid")
    if str(row.get("output_text") or "") != readable:
        raise ValueError("stored AI artifact readable output is inconsistent")
    return validated, readable


def _is_publisher_artifact(row: Mapping[str, object]) -> bool:
    """Check if an artifact row is a publisher-provided translation."""
    return str(row.get("provider") or "") == "publisher"


def _validated_publisher_output(
    row: Mapping[str, object],
) -> Tuple[Dict[str, object], str]:
    """Validate a publisher artifact's output without a PreparedTask.

    Publisher artifacts use fixed prompt/schema markers ('publisher', '') and
    don't go through AI model calls, so they cannot use the normal contract
    matching. This validates the output structure and CJK requirement.
    """
    task_type = str(row.get("task_type") or "")
    target_language = str(row.get("target_language") or "")
    input_scope = str(row.get("input_scope") or "")
    if task_type != "translation" or input_scope != "metadata":
        raise ValueError("publisher artifact must be a metadata translation")
    try:
        validated, readable = parse_and_validate_output(
            task_type,
            str(row.get("output_json") or ""),
            target_language=target_language,
            input_scope=input_scope,
            translated_fields=("title", "publisher_summary"),
        )
    except ValueError as exc:
        raise ValueError("publisher artifact output failed validation: %s" % exc) from exc
    output_json = canonical_json(validated)
    if str(row.get("output_json") or "") != output_json:
        raise ValueError("publisher artifact output is not canonical JSON")
    if str(row.get("output_hash") or "") != stable_hash(output_json):
        raise ValueError("publisher artifact output hash is invalid")
    if str(row.get("output_text") or "") != readable:
        raise ValueError("publisher artifact readable output is inconsistent")
    return validated, readable


def _publisher_artifact_fields(
    row: Mapping[str, object], output: Mapping[str, object]
) -> Dict[str, object]:
    """Return portable artifact fields for a publisher-provided translation."""
    return {
        "task_type": str(row["task_type"]),
        "input_scope": str(row["input_scope"]),
        "source_language": str(row["source_language"]),
        "target_language": str(row["target_language"]),
        "prompt_version": "publisher",
        "prompt_hash": stable_hash("publisher"),
        "response_schema_version": "publisher",
        "response_schema_hash": stable_hash("publisher"),
        "provider": "publisher",
        "requested_model": "publisher",
        "resolved_model": "publisher",
        "generation_params_hash": stable_hash("publisher"),
        "output": dict(output),
        "output_hash": stable_hash(canonical_json(output)),
        "input_truncated": False,
        "created_at": str(row["created_at"]),
    }


def _article_rows(connection: sqlite3.Connection) -> List[Dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT aa.*, a.source_slug, a.external_id, a.canonical_url,
                a.content_hash, s.name AS source_name
            FROM ai_artifacts aa
            JOIN articles a ON a.id=aa.article_id
            JOIN sources s ON s.slug=a.source_slug
            WHERE aa.status='succeeded'
              AND aa.task_type IN ('summary', 'translation')
              AND aa.input_scope='metadata'
              AND aa.source_artifact_id IS NULL
              AND aa.content_snapshot_id IS NULL
              AND aa.article_content_hash=a.content_hash
            ORDER BY a.source_slug, a.external_id, a.canonical_url,
                aa.task_type, aa.target_language, aa.created_at, aa.id
            """
        ).fetchall()
    ]


def _usage_attempt_rows(database: Database) -> List[Dict[str, object]]:
    """Read only the numeric/timestamp columns needed for public aggregation."""

    reader = getattr(database, "list_ai_usage_attempt_rows", None)
    if callable(reader):
        return [dict(row) for row in reader()]
    with database.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT request_started_at, reservation_active,
                    reserved_total_tokens, reserved_cost_micros,
                    actual_input_tokens, actual_cached_input_tokens,
                    actual_cache_write_tokens, actual_output_tokens,
                    actual_reasoning_tokens, actual_total_tokens,
                    actual_cost_micros
                FROM ai_attempts
                ORDER BY request_started_at
                """
            ).fetchall()
        ]


def _empty_usage_day(day_start: str, day_end: str) -> Dict[str, object]:
    result: Dict[str, object] = {
        "ledger_key": "",
        "timezone": AI_CACHE_USAGE_TIMEZONE,
        "day_start": day_start,
        "day_end": day_end,
        "covered_through": day_start,
    }
    result.update({field: 0 for field in _USAGE_INTEGER_FIELDS})
    return result


def _usage_entries(database: Database, exported_at: str) -> List[Dict[str, object]]:
    _, exported_datetime = _utc_timestamp(exported_at, "exported_at")
    zone = ZoneInfo(AI_CACHE_USAGE_TIMEZONE)
    earliest_local_day = (
        exported_datetime.astimezone(zone).date()
        - timedelta(days=AI_CACHE_MAX_USAGE_DAYS - 1)
    )
    by_day: Dict[str, Dict[str, object]] = {}
    prior_covered: Dict[str, datetime] = {}

    list_ledger = getattr(database, "list_ai_usage_ledger", None)
    imported_rows = list_ledger() if callable(list_ledger) else []
    for row_value in imported_rows:
        row = dict(row_value)
        if row.get("timezone") != AI_CACHE_USAGE_TIMEZONE:
            raise ValueError("stored AI usage ledger timezone is invalid")
        day_start, start = _utc_timestamp(
            row.get("day_start"), "stored usage day_start"
        )
        day_end, end = _utc_timestamp(row.get("day_end"), "stored usage day_end")
        _, covered = _utc_timestamp(
            row.get("covered_through"), "stored usage covered_through"
        )
        if _usage_day_window(start) != (day_start, day_end) or not start < covered:
            raise ValueError("stored AI usage ledger day boundary is invalid")
        if covered > exported_datetime:
            raise ValueError("stored AI usage ledger is ahead of the export clock")
        if start.astimezone(zone).date() < earliest_local_day:
            continue
        accumulator = _empty_usage_day(day_start, day_end)
        for field in _USAGE_INTEGER_FIELDS:
            accumulator[field] = _nonnegative_integer(
                row.get(field), "stored usage %s" % field
            )
        accumulator["covered_through"] = _utc_iso(
            min(covered, exported_datetime)
        )
        by_day[day_start] = accumulator
        prior_covered[day_start] = covered

    for attempt in _usage_attempt_rows(database):
        _, started = _utc_timestamp(
            attempt.get("request_started_at"), "AI attempt request_started_at"
        )
        if started > exported_datetime or started.astimezone(zone).date() < earliest_local_day:
            continue
        day_start, day_end = _usage_day_window(started)
        if started <= prior_covered.get(
            day_start, datetime.min.replace(tzinfo=timezone.utc)
        ):
            continue
        accumulator = by_day.setdefault(
            day_start, _empty_usage_day(day_start, day_end)
        )
        accumulator["requests"] = int(accumulator["requests"]) + 1
        reservation_active = bool(int(attempt.get("reservation_active") or 0))
        actual_total = attempt.get("actual_total_tokens")
        if reservation_active:
            accumulator["unconfirmed_requests"] = (
                int(accumulator["unconfirmed_requests"]) + 1
            )
            accumulator["reserved_total_tokens_for_unconfirmed"] = (
                int(accumulator["reserved_total_tokens_for_unconfirmed"])
                + max(0, int(attempt.get("reserved_total_tokens") or 0))
            )
            accumulator["reserved_cost_micros_for_unconfirmed"] = (
                int(accumulator["reserved_cost_micros_for_unconfirmed"])
                + max(0, int(attempt.get("reserved_cost_micros") or 0))
            )
            continue
        if actual_total is None:
            # A reservation released before a confirmed provider result still
            # consumes the conservative request counter, but no token/cost sum.
            accumulator["unconfirmed_requests"] = (
                int(accumulator["unconfirmed_requests"]) + 1
            )
            continue
        input_tokens = max(0, int(attempt.get("actual_input_tokens") or 0))
        cached_tokens = max(
            0, int(attempt.get("actual_cached_input_tokens") or 0)
        )
        cache_write_tokens = max(
            0, int(attempt.get("actual_cache_write_tokens") or 0)
        )
        output_tokens = max(0, int(attempt.get("actual_output_tokens") or 0))
        accumulator["confirmed_requests"] = (
            int(accumulator["confirmed_requests"]) + 1
        )
        accumulator["input_tokens"] = int(accumulator["input_tokens"]) + input_tokens
        accumulator["cached_input_tokens"] = (
            int(accumulator["cached_input_tokens"]) + cached_tokens
        )
        accumulator["cache_write_input_tokens"] = (
            int(accumulator["cache_write_input_tokens"]) + cache_write_tokens
        )
        accumulator["cache_miss_input_tokens"] = (
            int(accumulator["cache_miss_input_tokens"])
            + max(0, input_tokens - cached_tokens - cache_write_tokens)
        )
        accumulator["output_tokens"] = (
            int(accumulator["output_tokens"]) + output_tokens
        )
        accumulator["reasoning_tokens"] = (
            int(accumulator["reasoning_tokens"])
            + max(0, int(attempt.get("actual_reasoning_tokens") or 0))
        )
        accumulator["total_tokens"] = (
            int(accumulator["total_tokens"]) + max(0, int(actual_total))
        )
        accumulator["cost_micros"] = (
            int(accumulator["cost_micros"])
            + max(0, int(attempt.get("actual_cost_micros") or 0))
        )

    entries: List[Dict[str, object]] = []
    for day_start in sorted(by_day):
        entry = by_day[day_start]
        entry["covered_through"] = exported_at
        if int(entry["requests"]) <= 0:
            continue
        entry["ledger_key"] = _ledger_hash(entry)
        entries.append(entry)
    return entries


def _generation_hold_entries(
    database: Database,
    configured_sources: Sequence[SourceConfig],
    exported_at: str,
) -> Tuple[List[Dict[str, object]], int]:
    """Return only current, metadata-only holds that are safe to publish."""

    reader = getattr(database, "list_ai_generation_holds", None)
    if not callable(reader):
        return [], 0
    raw_entries = reader()
    configured_by_slug = {source.slug: source for source in configured_sources}
    _, exported_datetime = _utc_timestamp(exported_at, "exported_at")
    result: List[Dict[str, object]] = []
    skipped = 0
    with database.connect() as connection:
        for index, raw in enumerate(raw_entries):
            entry = dict(raw)
            try:
                validated = _validate_generation_hold_entry(
                    entry,
                    index=index,
                    configured_by_slug=configured_by_slug,
                    exported_datetime=exported_datetime,
                )
                descriptor = validated["descriptor"]
                for identity in descriptor["article_identities"]:
                    _resolve_identity(connection, identity)
            except (KeyError, TypeError, ValueError):
                # Full-text/legacy records and records for changed or removed
                # article versions must never enter the public handoff.
                skipped += 1
                continue
            result.append(entry)
    result.sort(key=lambda entry: str(entry["hold_key"]))
    return result, skipped


def export_ai_cache(
    database: Database,
    app_config: AppConfig,
    path: Path,
) -> Dict[str, object]:
    """Atomically publish all currently reusable public AI results."""

    service = AIService(app_config, database)
    artifacts: List[Dict[str, object]] = []
    skipped_incompatible = 0
    with database.connect() as connection:
        rows = _article_rows(connection)
    for row in rows:
        identity = _identity_from_row(row)
        if _is_publisher_artifact(row):
            try:
                output, _ = _validated_publisher_output(row)
            except ValueError:
                skipped_incompatible += 1
                continue
            entry: Dict[str, object] = {
                "cache_key": "",
                "article": identity,
                **_publisher_artifact_fields(row, output),
            }
            entry["cache_key"] = _entry_hash(entry)
            artifacts.append(entry)
            continue
        prepared = service.prepare_article(
            int(row["article_id"]),
            task_type=str(row["task_type"]),
            target_language=str(row["target_language"]),
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        if not _row_contract_matches_prepared(row, prepared):
            skipped_incompatible += 1
            continue
        output, _ = _validated_db_output(row, prepared)
        entry = {
            "cache_key": "",
            "article": identity,
            **_portable_artifact_fields(row, output),
        }
        entry["cache_key"] = _entry_hash(entry)
        artifacts.append(entry)

    latest_artifacts: Dict[Tuple[object, ...], Dict[str, object]] = {}
    for entry in artifacts:
        binding = (
            _identity_key(entry["article"]),
            str(entry["task_type"]),
            str(entry["target_language"]),
        )
        # Rows arrive oldest-to-newest.  A previous cache import can leave a
        # semantically equivalent result under a provider-specific local key;
        # publish exactly one deterministic, newest result per public binding.
        latest_artifacts[binding] = entry
    skipped_duplicate_artifacts = len(artifacts) - len(latest_artifacts)
    artifacts = list(latest_artifacts.values())
    artifacts.sort(
        key=lambda entry: (
            _identity_key(entry["article"]),
            str(entry["task_type"]),
            str(entry["target_language"]),
            str(entry["cache_key"]),
        )
    )
    exported_at = utc_now()
    usage_ledger = _usage_entries(database, exported_at)
    generation_holds, skipped_generation_holds = _generation_hold_entries(
        database,
        app_config.sources,
        exported_at,
    )
    payload: Dict[str, object] = {
        "protocol": AI_CACHE_PROTOCOL,
        "exported_at": exported_at,
        "bundle_hash": "0" * 64,
        "artifacts": artifacts,
        "usage_ledger": usage_ledger,
        "generation_holds": generation_holds,
    }
    _validate_payload(payload, app_config.sources, verify_hash=False)
    payload["bundle_hash"] = _cache_hash(payload)
    _validate_payload(payload, app_config.sources, verify_hash=True)
    destination = _atomic_write(Path(path), payload)
    return {
        "protocol": AI_CACHE_PROTOCOL,
        "path": str(destination),
        "bundle_hash": payload["bundle_hash"],
        "exported_at": payload["exported_at"],
        "article_artifacts": len(artifacts),
        "artifacts": len(artifacts),
        "usage_days": len(usage_ledger),
        "usage_requests": sum(int(entry["requests"]) for entry in usage_ledger),
        "generation_holds": len(generation_holds),
        "ambiguous_holds": sum(
            entry["hold_class"] == "ambiguous" for entry in generation_holds
        ),
        "paid_failure_holds": sum(
            entry["hold_class"] == "paid_failure" for entry in generation_holds
        ),
        "fallback_pending_holds": sum(
            entry["hold_class"] == "fallback_pending"
            for entry in generation_holds
        ),
        "skipped_generation_holds": skipped_generation_holds,
        "skipped_duplicate_artifacts": skipped_duplicate_artifacts,
        "skipped_incompatible": skipped_incompatible,
    }


def _resolve_identity(
    connection: sqlite3.Connection,
    identity: Mapping[str, object],
) -> Dict[str, object]:
    rows = connection.execute(
        """
        SELECT a.*, s.name AS source_name, s.home_url AS source_home_url
        FROM articles a JOIN sources s ON s.slug=a.source_slug
        WHERE a.source_slug=? AND (a.external_id=? OR a.canonical_url=?)
        ORDER BY a.id
        """,
        (
            identity["source_slug"],
            identity["external_id"],
            identity["canonical_url"],
        ),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("AI cache identity does not map to exactly one local article")
    row = dict(rows[0])
    if _identity_from_row(row) != dict(identity):
        raise ValueError("AI cache article identity or content_hash differs locally")
    return row


def _db_artifact(
    prepared: PreparedTask,
    portable: Mapping[str, object],
    validated_output: Mapping[str, object],
    readable: str,
) -> Dict[str, object]:
    output_json = canonical_json(validated_output)
    return {
        "article_id": prepared.article_id,
        "task_type": prepared.task_type,
        "input_scope": prepared.input_scope,
        "source_language": str(portable["source_language"]),
        "target_language": prepared.target_language,
        "artifact_key": prepared.artifact_key,
        "input_hash": prepared.input_hash,
        "article_content_hash": prepared.article_content_hash,
        "source_artifact_id": None,
        "content_snapshot_id": None,
        "prompt_version": prepared.definition.prompt_version,
        "prompt_hash": prepared.definition.prompt_hash,
        "response_schema_version": prepared.definition.schema_version,
        "response_schema_hash": prepared.definition.schema_hash,
        "provider": str(portable["provider"]),
        "requested_model": str(portable["requested_model"]),
        "resolved_model": str(portable["resolved_model"]),
        "generation_params_hash": str(portable["generation_params_hash"]),
        "provider_response_id": "",
        "output_json": output_json,
        "output_text": readable,
        "output_hash": stable_hash(output_json),
        "status": "succeeded",
        "input_truncated": int(bool(portable["input_truncated"])),
        "created_at": str(portable["created_at"]),
    }


def _db_publisher_artifact(
    article_id: int,
    article_content_hash: str,
    portable: Mapping[str, object],
    validated_output: Mapping[str, object],
    readable: str,
) -> Dict[str, object]:
    """Create a database artifact dict for a publisher-provided translation."""
    output_json = canonical_json(validated_output)
    target_language = str(portable["target_language"])
    artifact_key = stable_hash(
        "publisher-locale:%d:%s" % (article_id, target_language)
    )
    input_hash = stable_hash(
        "publisher:%d:%s" % (article_id, article_content_hash)
    )
    return {
        "article_id": article_id,
        "task_type": "translation",
        "input_scope": "metadata",
        "source_language": str(portable["source_language"]),
        "target_language": target_language,
        "artifact_key": artifact_key,
        "input_hash": input_hash,
        "article_content_hash": article_content_hash,
        "source_artifact_id": None,
        "content_snapshot_id": None,
        "prompt_version": "publisher",
        "prompt_hash": "",
        "response_schema_version": "publisher",
        "response_schema_hash": "",
        "provider": "publisher",
        "requested_model": "publisher",
        "resolved_model": "publisher",
        "generation_params_hash": "",
        "provider_response_id": "",
        "output_json": output_json,
        "output_text": readable,
        "output_hash": stable_hash(output_json),
        "status": "succeeded",
        "input_truncated": 0,
        "created_at": str(portable["created_at"]),
    }


def _insert_artifact(
    connection: sqlite3.Connection, artifact: Mapping[str, object]
) -> Tuple[int, bool]:
    values = [artifact[column] for column in _DB_ARTIFACT_COLUMNS]
    cursor = connection.execute(
        "INSERT OR IGNORE INTO ai_artifacts(%s) VALUES (%s)"
        % (
            ", ".join(_DB_ARTIFACT_COLUMNS),
            ", ".join("?" for _ in _DB_ARTIFACT_COLUMNS),
        ),
        values,
    )
    stored = connection.execute(
        "SELECT * FROM ai_artifacts WHERE artifact_key=?",
        (artifact["artifact_key"],),
    ).fetchone()
    if stored is None:
        raise sqlite3.IntegrityError("public AI cache artifact was not stored")
    for column in _DB_ARTIFACT_IMMUTABLE_COLUMNS:
        if stored[column] != artifact[column]:
            raise ValueError("public AI cache artifact key collision")
    return int(stored["id"]), cursor.rowcount == 0


def _artifact_appears_untranslated(
    entry: Mapping[str, object],
    row: Mapping[str, object],
) -> bool:
    """Check if a translation artifact appears untranslated vs the source article.

    Returns True if the artifact's output title or publisher_summary appears to
    be untranslated source text, based on comparing against the resolved article.
    """
    task_type = str(entry.get("task_type") or "")
    if task_type != "translation":
        return False

    target_language = str(entry.get("target_language") or "")
    output = entry.get("output")
    if not isinstance(output, dict):
        return False

    for field, source_key in (("title", "title"), ("publisher_summary", "summary")):
        source_text = row.get(source_key)
        output_text = output.get(field)
        if (
            isinstance(source_text, str)
            and isinstance(output_text, str)
            and source_text
            and output_text
            and _text_appears_untranslated(source_text, output_text, target_language)
        ):
            return True
    return False


def _is_publisher_portable_artifact(entry: Mapping[str, object]) -> bool:
    """Check if a portable artifact entry is a publisher-provided translation."""
    return str(entry.get("provider") or "") == "publisher"


def _resolved_payload(
    database: Database,
    app_config: AppConfig,
    payload: Mapping[str, object],
) -> Tuple[
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
    Dict[Tuple[str, str, str, str], int],
    int,
]:
    service = AIService(app_config, database)
    identity_rows: Dict[Tuple[str, str, str, str], Dict[str, object]] = {}
    with database.connect() as connection:
        for entry in payload["artifacts"]:  # type: ignore[index]
            identity = entry["article"]
            key = _identity_key(identity)
            if key not in identity_rows:
                identity_rows[key] = _resolve_identity(connection, identity)
        for hold in payload["generation_holds"]:  # type: ignore[index]
            for identity in hold["descriptor"]["article_identities"]:
                key = _identity_key(identity)
                if key not in identity_rows:
                    identity_rows[key] = _resolve_identity(connection, identity)

    artifacts: List[Dict[str, object]] = []
    local_key_seen = set()
    dropped_untranslated = 0
    for index, entry in enumerate(payload["artifacts"]):  # type: ignore[index]
        row = identity_rows[_identity_key(entry["article"])]
        article_id = int(row["id"])
        article_content_hash = str(entry["article"]["content_hash"])
        if str(row["content_hash"]) != article_content_hash:
            raise ValueError("AI cache article content_hash changed during import")
        if _is_publisher_portable_artifact(entry):
            if _artifact_appears_untranslated(entry, row):
                dropped_untranslated += 1
                continue
            validated, readable = _validate_article_output(
                entry, "artifacts[%d]" % index
            )
            artifact = _db_publisher_artifact(
                article_id, article_content_hash, entry, validated, readable
            )
            if artifact["artifact_key"] in local_key_seen:
                raise ValueError("AI cache entries map to a duplicate local artifact key")
            local_key_seen.add(artifact["artifact_key"])
            artifacts.append(artifact)
            continue
        prepared = service.prepare_article(
            article_id,
            task_type=str(entry["task_type"]),
            target_language=str(entry["target_language"]),
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        if prepared.article_content_hash != article_content_hash:
            raise ValueError("AI cache article content_hash changed during import")
        if not _prepared_contract_matches(prepared, entry):
            raise ValueError(
                "AI cache artifact is incompatible with the current prompt or schema"
            )
        if _artifact_appears_untranslated(entry, row):
            dropped_untranslated += 1
            continue
        validated, readable = _validate_article_output(
            entry, "artifacts[%d]" % index
        )
        artifact = _db_artifact(prepared, entry, validated, readable)
        if artifact["artifact_key"] in local_key_seen:
            raise ValueError("AI cache entries map to a duplicate local artifact key")
        local_key_seen.add(artifact["artifact_key"])
        artifacts.append(artifact)

    id_map = {
        identity: int(row["id"]) for identity, row in identity_rows.items()
    }
    usage_ledger = [
        {
            key: value
            for key, value in entry.items()
            if key != "ledger_key"
        }
        for entry in payload["usage_ledger"]  # type: ignore[index]
    ]
    generation_holds = [
        dict(entry)
        for entry in payload["generation_holds"]  # type: ignore[index]
    ]
    return artifacts, usage_ledger, generation_holds, id_map, dropped_untranslated


def _recheck_identity_map(
    connection: sqlite3.Connection,
    id_map: Mapping[Tuple[str, str, str, str], int],
) -> None:
    for identity, expected_id in id_map.items():
        row = connection.execute(
            """
            SELECT id, source_slug, external_id, canonical_url, content_hash
            FROM articles WHERE id=?
            """,
            (int(expected_id),),
        ).fetchone()
        if row is None or (
            str(row["source_slug"]),
            str(row["external_id"]),
            str(row["canonical_url"]),
            str(row["content_hash"]),
        ) != identity:
            raise ValueError("AI cache article identity changed during import")


def import_ai_cache(
    database: Database,
    app_config: AppConfig,
    path: Path,
) -> Dict[str, object]:
    """Strictly validate and atomically import a public AI cache bundle."""

    payload = _read_payload(Path(path))
    validated = _validate_payload(payload, app_config.sources, verify_hash=True)
    artifacts, usage_ledger, generation_holds, id_map, dropped_untranslated = (
        _resolved_payload(database, app_config, validated)
    )

    inserted_artifacts = 0
    artifact_cache_hits = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _recheck_identity_map(connection, id_map)
            replace_ledger = getattr(database, "_replace_ai_usage_ledger", None)
            if not callable(replace_ledger):
                if usage_ledger:
                    raise ValueError(
                        "database does not support imported AI usage ledgers"
                    )
            else:
                replace_ledger(connection, usage_ledger)
            replace_holds = getattr(database, "_replace_ai_generation_holds", None)
            if not callable(replace_holds):
                if generation_holds:
                    raise ValueError(
                        "database does not support imported AI generation holds"
                    )
            else:
                replace_holds(connection, generation_holds)
            for artifact in artifacts:
                _, cache_hit = _insert_artifact(connection, artifact)
                if cache_hit:
                    artifact_cache_hits += 1
                else:
                    inserted_artifacts += 1

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "protocol": AI_CACHE_PROTOCOL,
        "path": str(Path(path)),
        "bundle_hash": validated["bundle_hash"],
        "article_artifacts": len(artifacts),
        "usage_days": len(usage_ledger),
        "usage_requests": sum(
            int(entry["requests"]) for entry in usage_ledger
        ),
        "generation_holds": len(generation_holds),
        "ambiguous_holds": sum(
            entry["hold_class"] == "ambiguous" for entry in generation_holds
        ),
        "paid_failure_holds": sum(
            entry["hold_class"] == "paid_failure" for entry in generation_holds
        ),
        "fallback_pending_holds": sum(
            entry["hold_class"] == "fallback_pending"
            for entry in generation_holds
        ),
        "artifacts": len(artifacts),
        "inserted_artifacts": inserted_artifacts,
        "artifact_cache_hits": artifact_cache_hits,
        "dropped_untranslated": dropped_untranslated,
        "provider_api_calls": 0,
        "api_key_used": False,
    }


__all__ = [
    "AI_CACHE_MAX_BYTES",
    "AI_CACHE_PROTOCOL",
    "export_ai_cache",
    "import_ai_cache",
]
