"""Opt-in AI enrichment orchestration.

The deterministic sync path never imports this module.  Provider clients are
created lazily only after an explicit command or authenticated loopback web
action has passed feature checks, local-cache lookup, and an atomic budget
reservation.
"""

import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ai_prompts import (
    TaskDefinition,
    _text_appears_untranslated,
    canonical_json,
    parse_and_validate_output,
    stable_hash,
    task_definition,
)
from .ai_profiles import (
    DEFAULT_AI_FALLBACK_PROVIDER,
    DEFAULT_AI_PROVIDER,
    matches_ai_provider_profile,
)
from .ai_provider import (
    DeepSeekChatCompletionsProvider,
    OpenRouterChatCompletionsProvider,
    ProviderConfigError,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderRequest,
    ProviderResponse,
    ProviderUnknownError,
    ProviderUsage,
)
from .content import (
    CONTENT_EXTRACTOR_VERSION,
    ContentError,
    ContentFetcher,
    ContentSnapshot,
)
from .database import AIBudgetExceeded, AIJobConflict, Database, utc_now
from .i18n import normalize_language
from .models import AIConfig, AIModelPrice, AppConfig, SourceConfig


class AIServiceError(RuntimeError):
    """Base class for locally actionable AI enrichment errors."""


class AIDisabledError(AIServiceError):
    """The user has not opted in to model calls."""


class AIFeatureDisabledError(AIServiceError):
    """A requested AI capability is individually disabled."""


class AIInputError(AIServiceError, ValueError):
    """The selected article, language, scope, or input is invalid."""


class AIGenerationHeld(AIServiceError):
    """A stable generation fingerprint is held against automatic replay."""


class AIFallbackEligibleError(AIServiceError):
    """A classified primary-provider failure may continue on the fixed fallback."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        provider_call_made: bool,
        generation_hold_key: str = "",
    ) -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "provider_unavailable")[:100]
        self.provider_call_made = bool(provider_call_made)
        self.generation_hold_key = str(generation_hold_key or "")[:64]


TOKEN_ESTIMATE_PROTOCOL_MARGIN = 2_048
_FALLBACK_HTTP_STATUSES = frozenset({401, 402, 404, 429})
_TRANSIENT_KNOWN_PROVIDER_CODES = frozenset(
    {"provider_overloaded", "provider_unavailable", "rate_limit_exceeded"}
)
_FALLBACK_KNOWN_PROVIDER_CODES = frozenset(
    {
        # Stable OpenRouter typed availability failures.  Unknown codes and
        # content/auth/input categories are deliberately default-deny.
        *_TRANSIENT_KNOWN_PROVIDER_CODES,
        # Closed-profile contract violations with complete audited usage.
        "thinking_output",
        "thinking_tokens",
        "tool_calls",
        # Known unusable completions WITH confirmed usage.  The provider billed
        # us but returned no usable output; trying DeepSeek is safe because we
        # have exact usage data and are not risking a double-bill ambiguity.
        "no_output",
        "finish_length",
        "finish_error",
        "finish_reason",
        # Service-layer validation failures after a billed HTTP 200 response.
        "invalid_structured_output",
    }
)


@dataclass(frozen=True)
class PreparedTask:
    task_type: str
    article_id: Optional[int]
    input_scope: str
    target_language: str
    input_payload: Mapping[str, object]
    input_text: str
    input_hash: str
    artifact_key: str
    article_content_hash: str
    model: str
    max_output_tokens: int
    definition: TaskDefinition
    generation_params_hash: str
    input_truncated: bool
    content_snapshot_id: Optional[int]
    extractor_version: str
    expected_article_ids: Tuple[int, ...]
    translated_fields: Tuple[str, ...]

    def job_request(self) -> Dict[str, object]:
        return {
            "version": 1,
            "task_type": self.task_type,
            "article_id": self.article_id,
            "article_ids": list(self.expected_article_ids),
            "input_scope": self.input_scope,
            "target_language": self.target_language,
            "translated_fields": list(self.translated_fields),
            "expected_input_hash": self.input_hash,
            "expected_artifact_key": self.artifact_key,
            "expected_article_content_hash": self.article_content_hash,
            "content_snapshot_id": self.content_snapshot_id,
        }


@dataclass(frozen=True)
class _ObservedGenerationHold:
    hold_key: str
    hold_class: str
    revision: int
    legacy_pair: bool

    def delete_guard(self) -> Tuple[str, str, int]:
        return (self.hold_key, self.hold_class, self.revision)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc_datetime(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def conservative_token_estimate(*values: str) -> int:
    """Return a tokenizer-independent byte upper bound plus API overhead.

    Byte-fallback tokenizers cannot emit more content tokens than input bytes.
    The fixed margin covers request framing and provider-side prompt overhead
    that is not present in the user-visible strings.
    """

    combined = "\n".join(values)
    utf8_bytes = len(combined.encode("utf-8"))
    return max(1, utf8_bytes + TOKEN_ESTIMATE_PROTOCOL_MARGIN)


def _fit_payload(
    payload: Dict[str, object],
    max_characters: int,
    truncatable_fields: Sequence[str],
) -> Tuple[Dict[str, object], str, bool]:
    """Fit canonical JSON by shortening only explicitly named text fields."""

    result = dict(payload)
    encoded = canonical_json(result)
    if len(encoded) <= max_characters:
        return result, encoded, False
    truncated = False
    for field in truncatable_fields:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            continue
        low, high = 0, len(value)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate = value[:middle].rstrip()
            if middle < len(value) and candidate:
                candidate += "…"
            trial = dict(result)
            trial[field] = candidate
            if len(canonical_json(trial)) <= max_characters:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best != value:
            result[field] = best
            truncated = True
        encoded = canonical_json(result)
        if len(encoded) <= max_characters:
            return result, encoded, truncated
    raise AIInputError(
        "AI input metadata exceeds the configured character budget even after truncation"
    )


def _strict_json_object(value: str) -> Dict[str, object]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key: %s" % key)
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider output is not valid strict JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("provider output must be a JSON object")
    return decoded


class AIService:
    def __init__(
        self,
        app_config: AppConfig,
        database: Database,
        *,
        provider: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
        content_fetcher_factory: Optional[Callable[..., object]] = None,
        automatic_fallback_provider: str = "",
        allow_fallback_pending_from: str = "",
    ) -> None:
        self.app_config = app_config
        self.config: AIConfig = app_config.ai
        self.database = database
        self.sources = {source.slug: source for source in app_config.sources}
        self._provider_instance = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._content_fetcher_factory = content_fetcher_factory or ContentFetcher
        self._automatic_fallback_provider = str(
            automatic_fallback_provider or ""
        ).strip()
        self._allow_fallback_pending_from = str(
            allow_fallback_pending_from or ""
        ).strip()
        if self._automatic_fallback_provider and not (
            self.config.provider == DEFAULT_AI_PROVIDER
            and self._automatic_fallback_provider == DEFAULT_AI_FALLBACK_PROVIDER
        ):
            raise AIInputError(
                "automatic AI fallback only supports openrouter -> deepseek"
            )
        if self._allow_fallback_pending_from and not (
            self.config.provider == DEFAULT_AI_FALLBACK_PROVIDER
            and self._allow_fallback_pending_from == DEFAULT_AI_PROVIDER
        ):
            raise AIInputError(
                "fallback continuation only supports openrouter -> deepseek"
            )

    def _now(self) -> datetime:
        return _utc_datetime(self._clock())

    def _budget_window(self) -> Tuple[str, str, str, str]:
        try:
            local_zone = ZoneInfo(self.config.budget.timezone)
        except ZoneInfoNotFoundError as exc:
            raise AIInputError(
                "unknown AI budget timezone: %s" % self.config.budget.timezone
            ) from exc
        local_now = self._now().astimezone(local_zone)
        day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        next_day = day + timedelta(days=1)
        if month.month == 12:
            next_month = month.replace(year=month.year + 1, month=1)
        else:
            next_month = month.replace(month=month.month + 1)
        return _iso(day), _iso(month), _iso(next_day), _iso(next_month)

    def _budget_starts(self) -> Tuple[str, str]:
        daily_start, monthly_start, _, _ = self._budget_window()
        return daily_start, monthly_start

    def _provider(self) -> object:
        if self._provider_instance is None:
            options = {
                "timeout_seconds": self.config.timeout_seconds,
                "max_response_bytes": self.config.max_response_bytes,
            }
            if self.config.provider == "deepseek":
                self._provider_instance = DeepSeekChatCompletionsProvider(**options)
            elif self.config.provider == "openrouter":
                self._provider_instance = OpenRouterChatCompletionsProvider(**options)
            else:
                raise ProviderConfigError(
                    "unsupported AI provider: %s" % self.config.provider
                )
        return self._provider_instance

    def _require_enabled(self, task_type: str, input_scope: str) -> None:
        if not self.config.enabled:
            raise AIDisabledError(
                "AI is disabled; set config.ai.enabled=true before a token-consuming action"
            )
        feature = {
            "summary": self.config.summary_enabled,
            "translation": self.config.translation_enabled,
        }.get(task_type, False)
        if not feature:
            raise AIFeatureDisabledError("AI %s is disabled in configuration" % task_type)
        if input_scope == "full_text":
            if not self.config.full_text_enabled:
                raise AIFeatureDisabledError("AI full-text input is disabled in configuration")
            if self.config.input_policy == "metadata_only":
                raise AIFeatureDisabledError(
                    "config.ai.input_policy permits metadata only"
                )

    @staticmethod
    def _normalize_scope(value: str) -> str:
        normalized = str(value or "metadata").strip().lower().replace("-", "_")
        if normalized not in {"metadata", "full_text"}:
            raise AIInputError("unsupported AI input scope: %s" % value)
        return normalized

    @staticmethod
    def _normalize_fields(fields: Sequence[str]) -> Tuple[str, ...]:
        aliases = {
            "title": "title",
            "summary": "publisher_summary",
            "publisher-summary": "publisher_summary",
            "publisher_summary": "publisher_summary",
        }
        result: List[str] = []
        for field in fields:
            normalized = aliases.get(str(field).strip().lower())
            if normalized is None:
                raise AIInputError("unsupported translation field: %s" % field)
            if normalized not in result:
                result.append(normalized)
        if not result:
            raise AIInputError("translation requires at least one field")
        return tuple(result)

    def _article(self, article_id: int) -> Dict[str, object]:
        if isinstance(article_id, bool):
            raise AIInputError("article_id must be an integer")
        article = self.database.article(int(article_id))
        if article is None:
            raise AIInputError("article ID %s was not found" % article_id)
        return article

    def _portable_article_identity(self, article_id: int) -> Dict[str, str]:
        article = self._article(int(article_id))
        return {
            "source_slug": str(article.get("source_slug") or ""),
            "external_id": str(article.get("external_id") or ""),
            "canonical_url": str(article.get("canonical_url") or ""),
            "content_hash": str(article.get("content_hash") or ""),
        }

    def _portable_generation_input(self, value: object) -> object:
        """Replace ephemeral local article IDs with publisher identities."""

        if isinstance(value, Mapping):
            result: Dict[str, object] = {}
            article_id = value.get("article_id")
            for key, item in value.items():
                if key == "article_id":
                    continue
                result[str(key)] = self._portable_generation_input(item)
            if isinstance(article_id, int) and not isinstance(article_id, bool):
                result["article_identity"] = self._portable_article_identity(article_id)
            return result
        if isinstance(value, (list, tuple)):
            return [self._portable_generation_input(item) for item in value]
        return value

    def _generation_hold_template(
        self,
        prepared: PreparedTask,
        *,
        workload_kind: str,
    ) -> Dict[str, object]:
        portable_input = self._portable_generation_input(prepared.input_payload)
        descriptor: Dict[str, object] = {
            "protocol": "aaron-reader-ai-generation-hold/v1",
            "workload_kind": workload_kind,
            "provider": self.config.provider,
            "model": prepared.model,
            "task_type": prepared.task_type,
            "input_scope": prepared.input_scope,
            "target_language": prepared.target_language,
            "article_identities": [
                self._portable_article_identity(article_id)
                for article_id in prepared.expected_article_ids
            ],
            "portable_input_hash": stable_hash(portable_input),
            "prompt_version": prepared.definition.prompt_version,
            "prompt_hash": prepared.definition.prompt_hash,
            "schema_name": prepared.definition.schema_name,
            "schema_version": prepared.definition.schema_version,
            "schema_hash": prepared.definition.schema_hash,
            "generation_params_hash": prepared.generation_params_hash,
            "max_output_tokens": prepared.max_output_tokens,
        }
        return {
            "hold_key": stable_hash(descriptor),
            "workload_kind": workload_kind,
            "descriptor": descriptor,
        }

    @staticmethod
    def _classified_generation_hold(
        template: Mapping[str, object], hold_class: str
    ) -> Dict[str, object]:
        return {
            "hold_key": template["hold_key"],
            "workload_kind": template["workload_kind"],
            "hold_class": hold_class,
            "descriptor": template["descriptor"],
        }

    def _automatic_fallback_enabled(self) -> bool:
        return bool(
            self.config.provider == DEFAULT_AI_PROVIDER
            and self._automatic_fallback_provider
            == DEFAULT_AI_FALLBACK_PROVIDER
        )

    def _fallback_error(
        self,
        message: str,
        *,
        reason_code: str,
        provider_call_made: bool,
        generation_hold_key: str = "",
    ) -> AIServiceError:
        if self._automatic_fallback_enabled():
            return AIFallbackEligibleError(
                message,
                reason_code=reason_code,
                provider_call_made=provider_call_made,
                generation_hold_key=generation_hold_key,
            )
        return AIServiceError(message)

    @staticmethod
    def _known_failure_allows_fallback(code: object) -> bool:
        normalized = str(code or "").strip().lower()
        return normalized in _FALLBACK_KNOWN_PROVIDER_CODES

    @staticmethod
    def _http_failure_allows_fallback(status: object) -> bool:
        return (
            isinstance(status, int)
            and not isinstance(status, bool)
            and status in _FALLBACK_HTTP_STATUSES
        )

    @staticmethod
    def _http_failure_is_ambiguous(status: object) -> bool:
        return bool(
            isinstance(status, int)
            and not isinstance(status, bool)
            and (
                status in {408, 409, 425}
                or 500 <= status <= 599
            )
        )

    def _failure_generation_hold(
        self,
        template: Mapping[str, object],
        default_class: str,
        *,
        fallback_eligible: bool,
    ) -> Dict[str, object]:
        hold_class = (
            "fallback_pending"
            if fallback_eligible and self._automatic_fallback_enabled()
            else default_class
        )
        return self._classified_generation_hold(template, hold_class)

    @staticmethod
    def _generation_hold_semantic_identity(
        descriptor: Mapping[str, object],
    ) -> Dict[str, object]:
        """Identify the work independently of the selected provider profile.

        Provider and model remain in the stored descriptor for auditability,
        but changing either one must not bypass a hold for the same semantic
        generation.  The provider-specific generation hash is redundant with
        the separately retained prompt, schema, input, and output-limit fields.
        """

        ignored = {"provider", "model", "generation_params_hash"}
        return {
            str(key): value
            for key, value in descriptor.items()
            if key not in ignored
        }

    def _equivalent_generation_holds(
        self,
        template: Mapping[str, object],
    ) -> List[Dict[str, object]]:
        descriptor = template.get("descriptor")
        if not isinstance(descriptor, Mapping):
            return []
        identity = self._generation_hold_semantic_identity(descriptor)
        matches = []
        for hold in self.database.list_ai_generation_holds(
            include_revision=True
        ):
            candidate = hold.get("descriptor")
            if (
                isinstance(candidate, Mapping)
                and (
                    self._generation_hold_semantic_identity(candidate) == identity
                    or self._legacy_pair_hold_covers_translation(
                        descriptor,
                        candidate,
                    )
                )
            ):
                matches.append(hold)
        return matches

    @staticmethod
    def _legacy_pair_hold_covers_translation(
        requested: Mapping[str, object],
        candidate: Mapping[str, object],
    ) -> bool:
        """Bridge an older article-pair hold to its translation-only successor.

        The public product no longer generates per-article summaries, but an
        already billed pair request included the same title/summary translation.
        Matching the exact portable article version prevents that overlapping
        work from being replayed merely because the workload became narrower.
        """

        return bool(
            requested.get("workload_kind") == "article"
            and requested.get("task_type") == "translation"
            and requested.get("input_scope") == "metadata"
            and candidate.get("workload_kind") == "article_pair"
            and candidate.get("task_type") == "summary"
            and candidate.get("input_scope") == "metadata"
            and candidate.get("schema_name") == "article_summary_translation"
            and candidate.get("prompt_version") == "ai-enrichment-pair-v2"
            and candidate.get("target_language") == requested.get("target_language")
            and candidate.get("article_identities")
            == requested.get("article_identities")
        )

    @staticmethod
    def _all_paid_failure_holds(
        observed_holds: Sequence[_ObservedGenerationHold],
    ) -> bool:
        return bool(observed_holds) and all(
            observation.hold_class == "paid_failure"
            for observation in observed_holds
        )

    @staticmethod
    def _paid_failure_retry_authorized(
        observed_holds: Sequence[_ObservedGenerationHold],
    ) -> bool:
        """Keep a narrow paid replay valid through its fallback continuation."""

        return bool(
            any(
                observation.hold_class == "paid_failure"
                for observation in observed_holds
            )
            and all(
                observation.hold_class in {"paid_failure", "fallback_pending"}
                for observation in observed_holds
            )
        )

    @staticmethod
    def _exact_observed_generation_hold(
        template: Mapping[str, object],
        observed_holds: Sequence[_ObservedGenerationHold],
    ) -> Optional[_ObservedGenerationHold]:
        hold_key = str(template.get("hold_key") or "")
        return next(
            (
                observation
                for observation in observed_holds
                if observation.hold_key == hold_key
            ),
            None,
        )

    @staticmethod
    def _definitive_failure_may_settle_provisional(
        exact_hold: Optional[_ObservedGenerationHold],
        observed_holds: Sequence[_ObservedGenerationHold],
        *,
        retry_paid_failure: bool,
    ) -> bool:
        if any(
            observation.hold_class == "ambiguous"
            for observation in observed_holds
        ):
            return False
        if exact_hold is None:
            return True
        return bool(
            retry_paid_failure
            and exact_hold.hold_class == "paid_failure"
        )

    @staticmethod
    def _generation_holds_cleared_after_success(
        prepared: PreparedTask,
        observed_holds: Sequence[_ObservedGenerationHold],
    ) -> Tuple[Tuple[str, str, int], ...]:
        """Select preflight holds that a successful result fully settles.

        A direct fallback-pending or paid-failure hold represents exactly the
        requested task, so a successful generation settles it.  For translation
        tasks, ambiguous holds are also retired when a valid translation now
        exists, so they no longer block future same-hash regeneration.  A legacy
        article-pair hold is broader and may be settled only by the complete
        production translation (title plus publisher summary), never by a
        title-only or publisher-summary-only request.
        """

        complete_translation = bool(
            prepared.task_type == "translation"
            and set(prepared.translated_fields)
            == {"title", "publisher_summary"}
        )
        clearable_classes = {"fallback_pending", "paid_failure"}
        if prepared.task_type == "translation":
            clearable_classes.add("ambiguous")
        return tuple(
            observation.delete_guard()
            for observation in observed_holds
            if (
                complete_translation
                if observation.legacy_pair
                else observation.hold_class in clearable_classes
            )
        )

    def _check_generation_hold(
        self,
        template: Mapping[str, object],
        *,
        force_held: bool,
        retry_paid_failure: bool = False,
    ) -> Tuple[_ObservedGenerationHold, ...]:
        holds = self._equivalent_generation_holds(template)
        exact = self.database.ai_generation_hold(
            str(template["hold_key"]),
            include_revision=True,
        )
        if exact is not None and not any(
            str(hold.get("hold_key")) == str(exact.get("hold_key"))
            for hold in holds
        ):
            holds.append(exact)
        descriptor = template.get("descriptor")
        observed_by_key: Dict[str, _ObservedGenerationHold] = {}
        for hold in holds:
            hold_key = str(hold.get("hold_key") or "")
            hold_class = str(hold.get("hold_class") or "")
            revision = hold.get("revision")
            candidate_descriptor = hold.get("descriptor")
            if (
                not hold_key
                or not hold_class
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                continue
            observed_by_key[hold_key] = _ObservedGenerationHold(
                hold_key=hold_key,
                hold_class=hold_class,
                revision=revision,
                legacy_pair=bool(
                    isinstance(descriptor, Mapping)
                    and isinstance(candidate_descriptor, Mapping)
                    and self._legacy_pair_hold_covers_translation(
                        descriptor,
                        candidate_descriptor,
                    )
                ),
            )
        observed_holds = tuple(observed_by_key.values())
        if not holds:
            return observed_holds

        risk = {"fallback_pending": 0, "paid_failure": 1, "ambiguous": 2}
        ambiguous_holds = [
            candidate
            for candidate in holds
            if str(candidate.get("hold_class") or "") == "ambiguous"
        ]
        if ambiguous_holds and not force_held:
            hold = min(
                ambiguous_holds,
                key=lambda candidate: str(candidate.get("hold_key") or ""),
            )
            raise AIGenerationHeld(
                "AI generation is held against automatic replay (%s, %s)"
                % (hold.get("hold_class"), str(hold["hold_key"])[:12])
            )
        direct_holds = [
            candidate
            for candidate in holds
            if not (
                isinstance(descriptor, Mapping)
                and isinstance(candidate.get("descriptor"), Mapping)
                and self._legacy_pair_hold_covers_translation(
                    descriptor,
                    candidate["descriptor"],
                )
            )
        ]
        controlling_holds = direct_holds or holds
        pending_holds = [
            candidate
            for candidate in controlling_holds
            if str(candidate.get("hold_class")) == "fallback_pending"
        ]
        pending_can_continue = bool(pending_holds) and all(
            str(candidate.get("hold_class")) == "fallback_pending"
            or (
                retry_paid_failure
                and str(candidate.get("hold_class")) == "paid_failure"
            )
            for candidate in controlling_holds
        )
        pending_from_expected_provider = pending_can_continue and all(
            isinstance(candidate.get("descriptor"), Mapping)
            and str(candidate["descriptor"].get("provider") or "")
            == self._allow_fallback_pending_from
            for candidate in pending_holds
        )
        if (
            pending_from_expected_provider
            and self.config.provider == DEFAULT_AI_FALLBACK_PROVIDER
            and self._allow_fallback_pending_from == DEFAULT_AI_PROVIDER
        ):
            return observed_holds
        pending_from_primary_provider = pending_can_continue and all(
            isinstance(candidate.get("descriptor"), Mapping)
            and str(candidate["descriptor"].get("provider") or "")
            == self.config.provider
            for candidate in pending_holds
        )
        if (
            pending_from_primary_provider
            and self._automatic_fallback_enabled()
        ):
            hold = min(
                pending_holds,
                key=lambda candidate: str(candidate.get("hold_key") or ""),
            )
            raise AIFallbackEligibleError(
                "AI generation is waiting for the configured DeepSeek fallback",
                reason_code="fallback_pending",
                provider_call_made=False,
                generation_hold_key=str(hold.get("hold_key") or ""),
            )
        if force_held:
            return observed_holds
        if (
            retry_paid_failure
            and self._all_paid_failure_holds(observed_holds)
        ):
            return observed_holds
        hold = max(
            controlling_holds,
            key=lambda candidate: risk.get(
                str(candidate.get("hold_class") or ""), 3
            ),
        )
        raise AIGenerationHeld(
            "AI generation is held against automatic replay (%s, %s)"
            % (hold.get("hold_class"), str(hold["hold_key"])[:12])
        )

    def _generation_hold_preflight(
        self,
        template: Mapping[str, object],
        *,
        force_held: bool,
        retry_paid_failure: bool = False,
    ) -> Tuple[Tuple[_ObservedGenerationHold, ...], int]:
        """Read semantic holds behind a stable global revision fence.

        ``mark_ai_attempt_sent`` validates the returned revision in the same
        write transaction that installs the attempt's provisional hold.  The
        exact-key snapshot then identifies what that transaction may replace.
        Together they prevent a newly inserted equivalent legacy hold from
        slipping through the non-force ambiguous veto.
        """

        for _ in range(3):
            before = self.database.ai_generation_hold_revision_sequence()
            observed = self._check_generation_hold(
                template,
                force_held=force_held,
                retry_paid_failure=retry_paid_failure,
            )
            after = self.database.ai_generation_hold_revision_sequence()
            if before == after:
                return observed, after
        raise AIGenerationHeld(
            "AI generation holds changed during the final preflight"
        )

    def _clear_observed_generation_holds(
        self,
        holds: Sequence[_ObservedGenerationHold],
    ) -> None:
        """Clear only holds present before this provider request began.

        A semantically equivalent request can use a different provider-specific
        hold key.  Re-listing equivalent holds after success could therefore
        delete a hold installed by another request that is still in flight.
        The exact provisional hold for this attempt is cleared atomically by
        ``complete_ai_attempt``; this cleanup is limited to the preflight
        snapshot (for example, the primary fallback-pending hold).
        """

        for observation in holds:
            if observation.hold_class != "fallback_pending":
                continue
            self.database.clear_ai_generation_hold_if_snapshot(
                *observation.delete_guard(),
            )

    def _source_hosts(self, article: Mapping[str, object]) -> Tuple[str, ...]:
        source_slug = str(article.get("source_slug") or "")
        source = self.sources.get(source_slug)
        if source is None:
            raise AIInputError("article source is not present in current configuration")
        hosts = set()
        for value in (
            source.home_url,
            source.fetch_url,
            source.sitemap_url,
            source.metadata_url,
        ):
            if value:
                host = (urlsplit(value).hostname or "").rstrip(".").lower()
                if host:
                    hosts.add(host)
        article_host = (
            urlsplit(str(article.get("canonical_url") or "")).hostname or ""
        ).rstrip(".").lower()
        if not article_host or article_host not in hosts:
            raise AIInputError(
                "article URL host is not allowlisted by its current source configuration"
            )
        return tuple(sorted(hosts))

    def fetch_content(self, article_id: int, *, refresh: bool = False) -> Dict[str, object]:
        article = self._article(article_id)
        if not self.config.full_text_enabled:
            raise AIFeatureDisabledError("AI full-text input is disabled in configuration")
        if self.config.input_policy == "metadata_only":
            raise AIFeatureDisabledError("config.ai.input_policy permits metadata only")
        if self.config.input_policy == "fetch_on_demand_cached_local" and not refresh:
            cached = self.database.latest_content_snapshot(int(article_id))
            if (
                cached is not None
                and cached.get("canonical_url") == article.get("canonical_url")
                and cached.get("extractor_version") == CONTENT_EXTRACTOR_VERSION
            ):
                cached["cache_hit"] = True
                return cached

        fetcher = self._content_fetcher_factory(
            self._source_hosts(article),
            timeout_seconds=min(30, self.config.timeout_seconds),
            max_response_bytes=min(4_000_000, self.app_config.max_response_bytes),
            max_characters=self.config.max_full_text_chars,
        )
        fetch = getattr(fetcher, "fetch", None)
        if not callable(fetch):
            raise AIServiceError("content fetcher does not provide fetch(url)")
        try:
            snapshot = fetch(str(article["canonical_url"]))
        except ContentError as exc:
            raise AIInputError("article content could not be acquired safely: %s" % exc) from exc
        if not isinstance(snapshot, ContentSnapshot):
            raise AIServiceError("content fetcher returned an invalid snapshot")
        if not snapshot.text.strip():
            raise AIInputError("article content extraction returned no usable text")

        snapshot_data: Dict[str, object] = {
            "id": None,
            "article_id": int(article_id),
            "canonical_url": snapshot.source_url,
            "final_url": snapshot.final_url,
            "retrieved_at": snapshot.fetched_at,
            "content_type": snapshot.content_type,
            "etag": snapshot.etag,
            "last_modified": snapshot.last_modified,
            "extractor_version": snapshot.extractor_version,
            "normalized_text_hash": snapshot.text_sha256,
            "character_count": snapshot.character_count,
            "truncated": int(snapshot.truncated),
            "normalized_text": snapshot.text,
            "cache_hit": False,
        }
        if self.config.input_policy == "fetch_on_demand_cached_local":
            stored = self.database.store_content_snapshot(
                article_id=int(article_id),
                canonical_url=snapshot.source_url,
                final_url=snapshot.final_url,
                retrieved_at=snapshot.fetched_at,
                content_type=snapshot.content_type,
                etag=snapshot.etag,
                last_modified=snapshot.last_modified,
                extractor_version=snapshot.extractor_version,
                normalized_text_hash=snapshot.text_sha256,
                normalized_text=snapshot.text,
                truncated=snapshot.truncated,
            )
            stored["cache_hit"] = False
            return stored
        return snapshot_data

    def prepare_article(
        self,
        article_id: int,
        *,
        task_type: str,
        target_language: str = "zh-CN",
        input_scope: str = "metadata",
        translated_fields: Sequence[str] = ("title", "publisher_summary"),
        fetch_if_missing: bool = False,
    ) -> PreparedTask:
        if task_type not in {"summary", "translation"}:
            raise AIInputError("article task must be summary or translation")
        language = normalize_language(target_language)
        scope = self._normalize_scope(input_scope)
        fields = self._normalize_fields(translated_fields)
        if task_type == "translation" and scope != "metadata":
            raise AIInputError(
                "full article translation is intentionally unsupported; translate publisher metadata instead"
            )
        article = self._article(article_id)
        snapshot: Optional[Dict[str, object]] = None
        extractor_version = ""
        content_snapshot_id: Optional[int] = None
        input_truncated = False

        if task_type == "summary":
            payload: Dict[str, object] = {
                "article_id": int(article_id),
                "source": str(article.get("source_name") or article.get("source_slug") or ""),
                "title": str(article.get("title") or ""),
                "publisher_summary": str(article.get("summary") or ""),
                "published_at": article.get("published_at"),
                "url": str(article.get("canonical_url") or ""),
                "input_scope": scope,
                "target_language": language,
            }
            if scope == "full_text":
                snapshot = self.database.latest_content_snapshot(int(article_id))
                if snapshot is not None and (
                    snapshot.get("canonical_url") != article.get("canonical_url")
                    or snapshot.get("extractor_version")
                    != CONTENT_EXTRACTOR_VERSION
                ):
                    snapshot = None
                if snapshot is None and fetch_if_missing:
                    snapshot = self.fetch_content(int(article_id))
                if snapshot is None:
                    raise AIInputError(
                        "no extracted full-text snapshot exists; run ai fetch or allow on-demand fetching"
                    )
                text = str(snapshot.get("normalized_text") or "")
                if not text:
                    raise AIInputError("the extracted full-text snapshot is empty")
                payload["extracted_text"] = text
                extractor_version = str(snapshot.get("extractor_version") or "")
                content_snapshot_id = (
                    int(snapshot["id"]) if snapshot.get("id") is not None else None
                )
                max_chars = self.config.max_full_text_chars
                truncatable = ("extracted_text", "publisher_summary", "title")
                input_truncated = bool(snapshot.get("truncated"))
            else:
                max_chars = self.config.max_input_chars_per_article
                truncatable = ("publisher_summary", "title")
        else:
            payload = {
                "article_id": int(article_id),
                "source": str(article.get("source_name") or article.get("source_slug") or ""),
                "title": str(article.get("title") or "") if "title" in fields else None,
                "publisher_summary": (
                    str(article.get("summary") or "")
                    if "publisher_summary" in fields
                    else None
                ),
                "target_language": language,
            }
            max_chars = self.config.max_input_chars_per_article
            truncatable = ("publisher_summary", "title")

        fitted, input_text, fitted_truncated = _fit_payload(
            payload, max_chars, truncatable
        )
        input_truncated = input_truncated or fitted_truncated
        definition = task_definition(task_type)
        model, max_output = self._model_and_output(task_type)
        generation_hash = stable_hash(
            {
                "reasoning_effort": self.config.reasoning_effort,
                "max_output_tokens": max_output,
                "store": False,
            }
        )
        input_hash = stable_hash(input_text)
        artifact_key = stable_hash(
            {
                "article_id": int(article_id),
                "task_type": task_type,
                "input_scope": scope,
                "input_hash": input_hash,
                "article_content_hash": str(article.get("content_hash") or ""),
                "target_language": language,
                "translated_fields": list(fields) if task_type == "translation" else [],
                "prompt_hash": definition.prompt_hash,
                "schema_hash": definition.schema_hash,
                "provider": self.config.provider,
                "model": model,
                "generation_params_hash": generation_hash,
                "extractor_version": extractor_version,
            }
        )
        return PreparedTask(
            task_type=task_type,
            article_id=int(article_id),
            input_scope=scope,
            target_language=language,
            input_payload=fitted,
            input_text=input_text,
            input_hash=input_hash,
            artifact_key=artifact_key,
            article_content_hash=str(article.get("content_hash") or ""),
            model=model,
            max_output_tokens=max_output,
            definition=definition,
            generation_params_hash=generation_hash,
            input_truncated=input_truncated,
            content_snapshot_id=content_snapshot_id,
            extractor_version=extractor_version,
            expected_article_ids=(int(article_id),),
            translated_fields=fields if task_type == "translation" else tuple(),
        )

    def _model_and_output(self, task_type: str) -> Tuple[str, int]:
        if task_type == "summary":
            return self.config.summary_model, self.config.max_output_tokens_summary
        if task_type == "translation":
            return self.config.translation_model, self.config.max_output_tokens_translation
        raise AIInputError("unsupported AI task: %s" % task_type)

    def preview_article(self, article_id: int, **options: object) -> Dict[str, object]:
        prepared = self.prepare_article(article_id, **options)  # type: ignore[arg-type]
        return self.preview(prepared)

    def preview(self, prepared: PreparedTask) -> Dict[str, object]:
        estimate = conservative_token_estimate(
            prepared.definition.instructions,
            prepared.input_text,
            canonical_json(prepared.definition.schema),
        )
        cached = self.database.ai_artifact_by_key(prepared.artifact_key)
        daily_start, monthly_start = self._budget_starts()
        return {
            "task": prepared.task_type,
            "article_id": prepared.article_id,
            "article_ids": list(prepared.expected_article_ids),
            "input_scope": prepared.input_scope,
            "target_language": prepared.target_language,
            "model": prepared.model,
            "character_count": len(prepared.input_text),
            "utf8_bytes": len(prepared.input_text.encode("utf-8")),
            "estimated_input_tokens": estimate,
            "max_output_tokens": prepared.max_output_tokens,
            "estimated_max_total_tokens": estimate + prepared.max_output_tokens,
            "input_truncated": prepared.input_truncated,
            "input_hash": prepared.input_hash,
            "artifact_key": prepared.artifact_key,
            "cache_hit": cached is not None,
            "ai_enabled": self.config.enabled,
            "provider_will_be_called": False,
            "usage": self.database.ai_status(daily_start, monthly_start),
            "input": prepared.input_payload,
        }

    def _ensure_api_key(self) -> None:
        value = str(os.environ.get(self.config.api_key_environment, "")).strip()
        if not value:
            if self._automatic_fallback_enabled():
                raise AIFallbackEligibleError(
                    "%s is not set" % self.config.api_key_environment,
                    reason_code="missing_api_key",
                    provider_call_made=False,
                )
            raise ProviderConfigError(
                "%s is not set" % self.config.api_key_environment
            )

    def _price_reservation(self, prepared: PreparedTask, estimated_input: int) -> Tuple[int, Dict[str, object]]:
        price = self.config.prices.get(prepared.model)
        monetary_cap = any(
            value > 0
            for value in (
                self.config.budget.daily_max_cost_usd,
                self.config.budget.monthly_max_cost_usd,
            )
        )
        if monetary_cap and price is None:
            raise AIInputError(
                "a monetary cap is configured, but model %s has no explicit price snapshot"
                % prepared.model
            )
        if price is None:
            return 0, {}
        worst_input_rate = max(
            price.input_usd_per_million,
            price.cached_input_usd_per_million,
            price.cache_write_input_usd_per_million,
        )
        micros = int(
            math.ceil(
                estimated_input * worst_input_rate
                + prepared.max_output_tokens * price.output_usd_per_million
            )
        )
        return micros, asdict(price)

    @staticmethod
    def _actual_cost(price: Optional[AIModelPrice], usage: ProviderUsage) -> Optional[int]:
        if price is None:
            return None
        cached = max(0, usage.cached_input_tokens)
        cache_write = max(0, usage.cache_write_input_tokens)
        ordinary = max(0, usage.input_tokens - cached - cache_write)
        return int(
            math.ceil(
                ordinary * price.input_usd_per_million
                + cached * price.cached_input_usd_per_million
                + cache_write * price.cache_write_input_usd_per_million
                + usage.output_tokens * price.output_usd_per_million
            )
        )

    def enqueue(
        self,
        prepared: PreparedTask,
        *,
        priority: int = 100,
        trigger_kind: str = "cli",
        client_request_id: Optional[str] = None,
    ) -> Dict[str, object]:
        return self.database.ensure_ai_job(
            artifact_key=prepared.artifact_key,
            article_id=prepared.article_id,
            task_type=prepared.task_type,
            input_scope=prepared.input_scope,
            target_language=prepared.target_language,
            request=prepared.job_request(),
            priority=priority,
            trigger_kind=trigger_kind,
            max_attempts=self.config.batch.max_attempts,
            client_request_id=client_request_id,
        )

    def _cached_translation_is_valid(
        self,
        cached: Mapping[str, object],
        prepared: PreparedTask,
    ) -> bool:
        """防呆: Validate cached translation hasn't been detected as untranslated.

        Returns False if the cached output appears to be untranslated source text,
        which triggers a cache miss and regeneration with DeepSeek fallback.

        Fail-closed: JSON parse errors → False (cache miss).
        """
        if prepared.task_type != "translation":
            return True

        try:
            output_json = str(cached.get("output_json") or "{}")
            output = json.loads(output_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

        target_language = prepared.target_language
        for field in ("title", "publisher_summary"):
            if field not in prepared.translated_fields:
                continue
            source_text = prepared.input_payload.get(field)
            output_text = output.get(field)
            if (
                isinstance(source_text, str)
                and isinstance(output_text, str)
                and source_text
                and output_text
                and _text_appears_untranslated(source_text, output_text, target_language)
            ):
                return False
        return True

    def generate_article(
        self,
        article_id: int,
        *,
        task_type: str,
        target_language: str = "zh-CN",
        input_scope: str = "metadata",
        translated_fields: Sequence[str] = ("title", "publisher_summary"),
        trigger_kind: str = "cli",
        client_request_id: Optional[str] = None,
        force_held: bool = False,
        retry_paid_failure: bool = False,
    ) -> Dict[str, object]:
        scope = self._normalize_scope(input_scope)
        self._require_enabled(task_type, scope)
        prepared = self.prepare_article(
            article_id,
            task_type=task_type,
            target_language=target_language,
            input_scope=scope,
            translated_fields=translated_fields,
            fetch_if_missing=scope == "full_text",
        )
        cached = self.database.ai_artifact_by_key(prepared.artifact_key)
        if cached is not None and self._cached_translation_is_valid(cached, prepared):
            return self._artifact_result(cached, cache_hit=True)
        article_hold = self._generation_hold_template(
            prepared, workload_kind="article"
        )
        observed_generation_holds = self._check_generation_hold(
            article_hold,
            force_held=force_held,
            retry_paid_failure=retry_paid_failure,
        )
        retry_paid_failure_hold = bool(
            retry_paid_failure
            and self._paid_failure_retry_authorized(
                observed_generation_holds
            )
        )
        self._ensure_api_key()
        job = self.enqueue(
            prepared,
            priority=10,
            trigger_kind=trigger_kind,
            client_request_id=client_request_id,
        )
        if (
            job.get("state") == "cancelled"
            or (
                force_held
                and job.get("state") in {"unknown", "permanent_failed"}
            )
            or (
                retry_paid_failure_hold
                and job.get("state") in {"cancelled", "permanent_failed"}
            )
        ):
            job = self.database.requeue_ai_job(
                int(job["id"]), allow_unknown=force_held
            )
        return self.run_job(
            int(job["id"]),
            prepared_task=prepared,
            force_generation_hold=force_held,
            retry_paid_failure_hold=retry_paid_failure_hold,
        )

    def current_article_artifact(
        self,
        article_id: int,
        *,
        task_type: str,
        target_language: str = "zh-CN",
        input_scope: str = "metadata",
    ) -> Optional[Dict[str, object]]:
        """Return current coverage regardless of the producing provider/model.

        For translation tasks, validates that the cached output actually appears
        translated (contains CJK for zh-* targets).  An untranslated cached
        artifact is treated as a miss so the cloud-run scan can regenerate it.
        """

        if task_type not in {"summary", "translation"}:
            raise AIInputError("article task must be summary or translation")
        language = normalize_language(target_language)
        scope = self._normalize_scope(input_scope)
        candidates = self.database.latest_ai_artifacts([int(article_id)]).get(
            int(article_id), []
        )
        for artifact in candidates:
            if (
                artifact.get("task_type") == task_type
                and artifact.get("target_language") == language
                and artifact.get("input_scope") == scope
            ):
                if task_type == "translation" and not self._cached_artifact_is_translated(
                    artifact, int(article_id), language
                ):
                    continue
                return self._artifact_result(artifact, cache_hit=True)
        return None

    def _cached_artifact_is_translated(
        self,
        artifact: Mapping[str, object],
        article_id: int,
        target_language: str,
    ) -> bool:
        """防呆: Validate cached translation actually appears translated.

        Returns False if the cached output appears to be untranslated source
        text (e.g. Latin-only title for a zh-CN target), which triggers a cache
        miss so cloud-run can regenerate it with DeepSeek fallback.

        Fail-closed: JSON parse errors or missing article → False (cache miss).
        """
        try:
            output_json = str(artifact.get("output_json") or "{}")
            output = json.loads(output_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

        article = self.database.article(article_id)
        if article is None:
            return False

        for field, source_key in (("title", "title"), ("publisher_summary", "summary")):
            source_text = article.get(source_key)
            output_text = output.get(field)
            if (
                isinstance(source_text, str)
                and isinstance(output_text, str)
                and source_text
                and output_text
                and _text_appears_untranslated(source_text, output_text, target_language)
            ):
                return False
        return True

    def generate_article_pair(
        self,
        article_id: int,
        *,
        target_language: str = "zh-CN",
        trigger_kind: str = "cloud",
        force_held: bool = False,
    ) -> Dict[str, object]:
        """Generate a summary and metadata translation with one provider call."""

        if (
            self.config.reasoning_effort != "none"
            or self.config.store
            or not matches_ai_provider_profile(
                self.config.provider,
                (
                    self.config.summary_model,
                    self.config.translation_model,
                ),
                self.config.api_key_environment,
            )
        ):
            raise AIInputError(
                "combined article generation requires a fixed cloud AI profile"
            )
        language = normalize_language(target_language)
        self._require_enabled("summary", "metadata")
        self._require_enabled("translation", "metadata")
        summary = self.prepare_article(
            article_id,
            task_type="summary",
            target_language=language,
            input_scope="metadata",
        )
        translation = self.prepare_article(
            article_id,
            task_type="translation",
            target_language=language,
            input_scope="metadata",
            translated_fields=("title", "publisher_summary"),
        )
        if summary.article_content_hash != translation.article_content_hash:
            raise AIInputError("article changed while preparing combined AI input")

        cached_summary = self.current_article_artifact(
            article_id,
            task_type="summary",
            target_language=language,
        )
        cached_translation_artifact = self.database.ai_artifact_by_key(
            translation.artifact_key
        )

        # 防呆: Validate cached translation hasn't been detected as untranslated.
        # If invalid, treat as cache miss and regenerate with DeepSeek fallback.
        cached_translation: Optional[Dict[str, object]] = None
        if cached_translation_artifact is not None:
            if self._cached_translation_is_valid(cached_translation_artifact, translation):
                cached_translation = self._artifact_result(
                    cached_translation_artifact, cache_hit=True
                )

        if cached_summary is not None and cached_translation is not None:
            return {
                "summary": cached_summary,
                "translation": cached_translation,
                "cache_hit": True,
                "provider_api_calls": 0,
            }
        if cached_summary is not None or cached_translation is not None:
            missing_task = (
                "translation" if cached_summary is not None else "summary"
            )
            generated = self.generate_article(
                article_id,
                task_type=missing_task,
                target_language=language,
                input_scope="metadata",
                translated_fields=("title", "publisher_summary"),
                trigger_kind=trigger_kind,
                force_held=force_held,
            )
            return {
                "summary": cached_summary or generated,
                "translation": cached_translation or generated,
                "cache_hit": bool(generated.get("cache_hit")),
                "provider_api_calls": 0 if generated.get("cache_hit") else 1,
            }

        combined_schema: Dict[str, object] = {
            "type": "object",
            "properties": {
                "summary": dict(summary.definition.schema),
                "translation": dict(translation.definition.schema),
            },
            "required": ["summary", "translation"],
            "additionalProperties": False,
        }
        combined_instructions = (
            "%s\n\nUse the single article object for the summary. Translate only "
            "article.title and article.publisher_summary without summarizing or "
            "expanding them. If translation_overrides contains either field, use "
            "that value for the translation only. Return one top-level JSON object "
            "with exactly summary and translation; each nested value must match its "
            "corresponding response schema."
            % summary.definition.instructions
        )
        combined_definition = TaskDefinition(
            task_type="summary",
            instructions=combined_instructions,
            schema_name="article_summary_translation",
            schema=combined_schema,
            prompt_version="ai-enrichment-pair-v2",
            prompt_hash=stable_hash(combined_instructions),
            schema_version="ai-output-pair-v1",
            schema_hash=stable_hash(combined_schema),
        )
        combined_max_output = (
            summary.max_output_tokens + translation.max_output_tokens
        )
        if self.config.provider == "deepseek":
            # Preserve the established DeepSeek hold identity exactly.  A
            # provider addition must not make an older ambiguous request look
            # like fresh work that is safe to replay.
            generation_profile = {
                "profile": "deepseek-cloud-nonthinking-json-v1",
                "thinking": "disabled",
                "max_output_tokens": combined_max_output,
                "store_sent": False,
                "tools_sent": False,
            }
            bundle_protocol = "deepseek-article-pair/v2"
        else:
            generation_profile = {
                "profile": "openrouter-free-cloud-nonthinking-json-v1",
                "reasoning": "none",
                "max_output_tokens": combined_max_output,
                "store_sent": False,
                "tools_sent": False,
            }
            bundle_protocol = "openrouter-free-article-pair/v1"
        combined_generation_hash = stable_hash(generation_profile)
        bundle_key = stable_hash(
            {
                "protocol": bundle_protocol,
                "summary_artifact_key": summary.artifact_key,
                "translation_artifact_key": translation.artifact_key,
                "prompt_hash": combined_definition.prompt_hash,
                "schema_hash": combined_definition.schema_hash,
                "generation_params_hash": combined_generation_hash,
            }
        )
        shared_article = {
            key: value
            for key, value in summary.input_payload.items()
            if key not in {"input_scope", "target_language"}
        }
        translation_overrides = {
            field: translation.input_payload.get(field)
            for field in ("title", "publisher_summary")
            if translation.input_payload.get(field) != shared_article.get(field)
        }
        combined_input_payload: Dict[str, object] = {
            "input_scope": "metadata",
            "target_language": language,
            "article": shared_article,
        }
        if translation_overrides:
            combined_input_payload["translation_overrides"] = translation_overrides
        combined_input_text = canonical_json(combined_input_payload)
        bundle = replace(
            summary,
            input_payload=combined_input_payload,
            input_text=combined_input_text,
            input_hash=stable_hash(combined_input_text),
            artifact_key=bundle_key,
            max_output_tokens=combined_max_output,
            definition=combined_definition,
            generation_params_hash=combined_generation_hash,
        )
        generation_hold = self._generation_hold_template(
            bundle,
            workload_kind="article_pair",
        )
        (
            observed_generation_holds,
            preflight_generation_hold_revision,
        ) = self._generation_hold_preflight(
            generation_hold,
            force_held=force_held,
        )
        exact_generation_hold = self._exact_observed_generation_hold(
            generation_hold,
            observed_generation_holds,
        )
        preflight_generation_hold_snapshot = (
            exact_generation_hold.delete_guard()
            if exact_generation_hold is not None
            else None
        )
        settle_definitive_failure = (
            self._definitive_failure_may_settle_provisional(
                exact_generation_hold,
                observed_generation_holds,
                retry_paid_failure=force_held,
            )
        )
        self._ensure_api_key()
        request_payload = bundle.job_request()
        request_payload.update(
            {
                "bundle": "summary_translation_v1",
                "summary_artifact_key": summary.artifact_key,
                "translation_artifact_key": translation.artifact_key,
            }
        )
        job = self.database.ensure_ai_job(
            artifact_key=bundle_key,
            article_id=int(article_id),
            task_type="summary",
            input_scope="metadata",
            target_language=language,
            request=request_payload,
            priority=10,
            trigger_kind=trigger_kind,
            max_attempts=1,
        )
        if force_held and job.get("state") in {
            "unknown",
            "permanent_failed",
            "cancelled",
        }:
            job = self.database.requeue_ai_job(
                int(job["id"]), allow_unknown=True
            )
        if job.get("state") in {"unknown", "permanent_failed", "cancelled"}:
            raise AIServiceError(
                "combined AI job %s cannot run from state %s"
                % (job["id"], job.get("state"))
            )
        if job.get("state") == "succeeded":
            # Successful pair jobs are only committed with both artifacts.
            refreshed_summary = self.current_article_artifact(
                article_id, task_type="summary", target_language=language
            )
            refreshed_translation = self.current_article_artifact(
                article_id, task_type="translation", target_language=language
            )
            if refreshed_summary is None or refreshed_translation is None:
                raise AIServiceError("combined AI job is missing a committed artifact")
            return {
                "summary": refreshed_summary,
                "translation": refreshed_translation,
                "cache_hit": True,
                "provider_api_calls": 0,
            }

        estimated_input = conservative_token_estimate(
            combined_definition.instructions,
            bundle.input_text,
            canonical_json(combined_schema),
        )
        reserved_cost, price_snapshot = self._price_reservation(
            bundle, estimated_input
        )
        daily_start, monthly_start, daily_reset, monthly_reset = self._budget_window()
        idempotency_key = self._provider_idempotency_key(
            job_id=int(job["id"]),
            next_attempt=int(job.get("attempt_count") or 0) + 1,
            artifact_key=bundle_key,
        )
        budget = self.config.budget
        reservation = self.database.reserve_ai_attempt(
            job_id=int(job["id"]),
            idempotency_key=idempotency_key,
            requested_provider=self.config.provider,
            requested_model=bundle.model,
            estimated_input_tokens=estimated_input,
            reserved_output_tokens=combined_max_output,
            reserved_cost_micros=reserved_cost,
            price_snapshot=price_snapshot,
            daily_started_at=daily_start,
            monthly_started_at=monthly_start,
            daily_reset_at=daily_reset,
            monthly_reset_at=monthly_reset,
            daily_max_requests=budget.daily_max_requests,
            daily_max_total_tokens=budget.daily_max_total_tokens,
            daily_max_cost_micros=int(
                round(budget.daily_max_cost_usd * 1_000_000)
            ),
            monthly_max_requests=budget.monthly_max_requests,
            monthly_max_total_tokens=budget.monthly_max_total_tokens,
            monthly_max_cost_micros=int(
                round(budget.monthly_max_cost_usd * 1_000_000)
            ),
        )
        if reservation.get("cache_hit"):
            raise AIServiceError("combined AI cache is inconsistent")
        attempt_id = int(reservation["id"])
        provider_request = ProviderRequest(
            model=bundle.model,
            instructions=combined_definition.instructions,
            input_text=bundle.input_text,
            json_schema=combined_schema,
            schema_name=combined_definition.schema_name,
            idempotency_key=idempotency_key,
            max_output_tokens=combined_max_output,
            reasoning_effort="none",
        )
        response, provisional_generation_hold = self._call_provider_for_attempt(
            attempt_id=attempt_id,
            prepared=bundle,
            provider_request=provider_request,
            generation_hold=generation_hold,
            preflight_generation_hold_snapshot=(
                preflight_generation_hold_snapshot
            ),
            preflight_generation_hold_revision=(
                preflight_generation_hold_revision
            ),
            settle_definitive_failure=settle_definitive_failure,
        )
        usage_dict = self._usage_dict(response.usage)
        usage_confirmed = bool(
            response.usage_reported and usage_dict.get("total_tokens", 0) > 0
        )
        actual_cost = (
            self._actual_cost(self.config.prices.get(bundle.model), response.usage)
            if usage_confirmed
            else None
        )
        try:
            combined_output = _strict_json_object(response.output_text)
            if set(combined_output) != {"summary", "translation"}:
                raise ValueError(
                    "combined output fields do not match the response contract"
                )
            validated_summary, readable_summary = parse_and_validate_output(
                "summary",
                canonical_json(combined_output["summary"]),
                target_language=language,
                input_scope="metadata",
            )
            validated_translation, readable_translation = parse_and_validate_output(
                "translation",
                canonical_json(combined_output["translation"]),
                target_language=language,
                input_scope="metadata",
                translated_fields=("title", "publisher_summary"),
                translation_input=translation.input_payload,
            )
        except ValueError as exc:
            fallback_eligible = bool(
                usage_confirmed and self._automatic_fallback_enabled()
            )
            self.database.fail_ai_attempt(
                attempt_id=attempt_id,
                job_state="permanent_failed",
                error_class="output_validation",
                error_code="invalid_structured_output",
                error_message=str(exc),
                http_status=200,
                usage=usage_dict if usage_confirmed else None,
                actual_cost_micros=actual_cost,
                preserve_reservation=not usage_confirmed,
                generation_hold=self._failure_generation_hold(
                    generation_hold,
                    "paid_failure" if usage_confirmed else "ambiguous",
                    fallback_eligible=fallback_eligible,
                ),
                settle_provisional_generation_hold=(
                    provisional_generation_hold
                    if usage_confirmed and settle_definitive_failure
                    else None
                ),
            )
            message = "provider output failed local validation: %s" % exc
            error = (
                self._fallback_error(
                    message,
                    reason_code="invalid_structured_output",
                    provider_call_made=True,
                    generation_hold_key=str(
                        generation_hold.get("hold_key") or ""
                    ),
                )
                if fallback_eligible
                else AIServiceError(message)
            )
            raise error from exc

        summary_artifact = self._provider_artifact(
            summary,
            validated=validated_summary,
            readable=readable_summary,
            response=response,
            usage_confirmed=usage_confirmed,
            response_text=response.output_text,
        )
        translation_artifact = self._provider_artifact(
            translation,
            validated=validated_translation,
            readable=readable_translation,
            response=response,
            usage_confirmed=usage_confirmed,
            response_text=response.output_text,
        )
        stored_summary = self.database.complete_ai_attempt(
            attempt_id=attempt_id,
            artifact=summary_artifact,
            additional_artifacts=(translation_artifact,),
            usage=usage_dict,
            actual_cost_micros=actual_cost,
            usage_confirmed=usage_confirmed,
            clear_generation_hold_snapshots=(
                (provisional_generation_hold,)
                + self._generation_holds_cleared_after_success(
                    bundle,
                    observed_generation_holds,
                )
            ),
        )
        stored_translation = self.database.ai_artifact_by_key(
            translation.artifact_key
        )
        if stored_translation is None:
            raise AIServiceError("combined translation artifact was not committed")
        return {
            "summary": self._artifact_result(
                stored_summary, cache_hit=False
            ),
            "translation": self._artifact_result(
                stored_translation, cache_hit=False
            ),
            "cache_hit": False,
            "provider_api_calls": 1,
        }

    def _prepare_from_job(
        self, job: Mapping[str, object], *, fetch_if_missing: bool = False
    ) -> PreparedTask:
        try:
            request = json.loads(str(job["request_json"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIInputError("AI job request is invalid") from exc
        if not isinstance(request, dict) or request.get("version") != 1:
            raise AIInputError("AI job request version is unsupported")
        task_type = str(request.get("task_type") or "")
        prepared = self.prepare_article(
            int(request.get("article_id")),
            task_type=task_type,
            target_language=str(request.get("target_language") or ""),
            input_scope=str(request.get("input_scope") or "metadata"),
            translated_fields=tuple(request.get("translated_fields") or ("title", "publisher_summary")),
            fetch_if_missing=fetch_if_missing,
        )
        if prepared.input_hash != request.get("expected_input_hash"):
            raise AIInputError("article input changed after this AI job was queued")
        if prepared.artifact_key != request.get("expected_artifact_key"):
            raise AIInputError("AI task configuration changed after this job was queued")
        return prepared

    def _provider_idempotency_key(
        self,
        *,
        job_id: int,
        next_attempt: int,
        artifact_key: str,
    ) -> str:
        """Return a fresh audited key for every potentially billable request."""

        return "ar-%d-g%d-%s" % (
            int(job_id),
            int(next_attempt),
            artifact_key[:20],
        )

    def _call_provider_for_attempt(
        self,
        *,
        attempt_id: int,
        prepared: PreparedTask,
        provider_request: ProviderRequest,
        generation_hold: Mapping[str, object],
        preflight_generation_hold_snapshot: Optional[
            Tuple[str, str, int]
        ],
        preflight_generation_hold_revision: int,
        settle_definitive_failure: bool,
    ) -> Tuple[ProviderResponse, Tuple[str, str, int]]:
        """Make one audited provider call without retrying ambiguous failures."""

        try:
            generate = getattr(self._provider(), "generate", None)
            if not callable(generate):
                raise ProviderConfigError(
                    "AI provider does not provide generate(request)"
                )
        except ProviderConfigError as exc:
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="permanent_failed",
                error_class="provider_config",
                error_code="provider_config",
                error_message=str(exc),
            )
            raise

        provisional_hold = self._classified_generation_hold(
            generation_hold, "ambiguous"
        )
        try:
            provisional_snapshot = self.database.mark_ai_attempt_sent(
                int(attempt_id),
                provisional_generation_hold=provisional_hold,
                preflight_generation_hold_snapshot=(
                    preflight_generation_hold_snapshot
                ),
                preflight_generation_hold_revision=(
                    preflight_generation_hold_revision
                ),
            )
        except AIJobConflict as exc:
            message = "AI generation holds changed after the final preflight"
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="cancelled",
                error_class="generation_hold_conflict",
                error_code="generation_hold_changed",
                error_message=message,
            )
            raise AIGenerationHeld(message) from exc

        def restore_or_clear_preflight_hold() -> Dict[str, object]:
            if preflight_generation_hold_snapshot is None:
                return {
                    "generation_hold": None,
                    "settle_provisional_generation_hold": None,
                    "clear_provisional_generation_hold": provisional_snapshot,
                }
            return {
                "generation_hold": self._classified_generation_hold(
                    generation_hold,
                    preflight_generation_hold_snapshot[1],
                ),
                "settle_provisional_generation_hold": provisional_snapshot,
                "clear_provisional_generation_hold": None,
            }

        try:
            response = generate(provider_request)
            if not isinstance(response, ProviderResponse):
                raise ProviderUnknownError(
                    "provider returned an invalid response; "
                    "the request may already have been processed"
                )
            return response, provisional_snapshot
        except ProviderHTTPError as exc:
            # No configured provider promises safe replay for a rejected POST.
            # A 429 is a definitive rejection; transport-like HTTP failures may
            # already have reached inference and therefore remain unknown.
            ambiguous_result = self._http_failure_is_ambiguous(exc.status)
            fallback_eligible = bool(
                self._automatic_fallback_enabled()
                and self._http_failure_allows_fallback(exc.status)
            )
            transient_rejection = bool(
                not fallback_eligible
                and self._http_failure_allows_fallback(exc.status)
            )
            if transient_rejection:
                hold_transition = restore_or_clear_preflight_hold()
            else:
                hold_transition = {
                    "generation_hold": self._failure_generation_hold(
                        generation_hold,
                        "ambiguous" if ambiguous_result else "paid_failure",
                        fallback_eligible=fallback_eligible,
                    ),
                    "settle_provisional_generation_hold": (
                        provisional_snapshot
                        if not ambiguous_result and settle_definitive_failure
                        else None
                    ),
                    "clear_provisional_generation_hold": None,
                }
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="unknown" if ambiguous_result else "permanent_failed",
                error_class=(
                    "provider_http_unknown"
                    if ambiguous_result
                    else "provider_http"
                ),
                error_code="http_%d" % exc.status,
                error_message=str(exc),
                http_status=exc.status,
                next_attempt_at=None,
                generation_hold=hold_transition["generation_hold"],
                settle_provisional_generation_hold=hold_transition[
                    "settle_provisional_generation_hold"
                ],
                clear_provisional_generation_hold=hold_transition[
                    "clear_provisional_generation_hold"
                ],
            )
            error = self._fallback_error(
                str(exc),
                reason_code="http_%d" % exc.status,
                provider_call_made=True,
                generation_hold_key=(
                    str(generation_hold.get("hold_key") or "")
                    if fallback_eligible
                    else ""
                ),
            ) if fallback_eligible else AIServiceError(str(exc))
            raise error from exc
        except ProviderKnownError as exc:
            known_usage = self._usage_dict(exc.usage)
            known_cost = self._actual_cost(
                self.config.prices.get(prepared.model), exc.usage
            )
            fallback_eligible = bool(
                self._automatic_fallback_enabled()
                and self._known_failure_allows_fallback(exc.code)
            )
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="permanent_failed",
                error_class="provider_known",
                error_code=exc.code,
                error_message=str(exc),
                http_status=200,
                usage=known_usage,
                actual_cost_micros=known_cost,
                provider_request_id=exc.request_id or exc.response_id,
                resolved_model=exc.model,
                finish_reason=exc.code,
                generation_hold=self._failure_generation_hold(
                    generation_hold,
                    "paid_failure",
                    fallback_eligible=fallback_eligible,
                ),
                settle_provisional_generation_hold=(
                    provisional_snapshot
                    if settle_definitive_failure
                    else None
                ),
            )
            error = (
                self._fallback_error(
                    str(exc),
                    reason_code=str(exc.code or "known_unusable_completion"),
                    provider_call_made=True,
                    generation_hold_key=str(
                        generation_hold.get("hold_key") or ""
                    ),
                )
                if fallback_eligible
                else AIServiceError(str(exc))
            )
            raise error from exc
        except ProviderUnknownError as exc:
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="unknown",
                error_class="provider_unknown",
                error_code="unknown_result",
                error_message=str(exc),
                generation_hold=self._classified_generation_hold(
                    generation_hold, "ambiguous"
                ),
            )
            raise AIServiceError(str(exc)) from exc
        except ProviderConfigError as exc:
            hold_transition = restore_or_clear_preflight_hold()
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="permanent_failed",
                error_class="provider_config",
                error_code="provider_config",
                error_message=str(exc),
                generation_hold=hold_transition["generation_hold"],
                settle_provisional_generation_hold=hold_transition[
                    "settle_provisional_generation_hold"
                ],
                clear_provisional_generation_hold=hold_transition[
                    "clear_provisional_generation_hold"
                ],
            )
            raise
        except Exception as exc:
            self.database.fail_ai_attempt(
                attempt_id=int(attempt_id),
                job_state="unknown",
                error_class="provider_unexpected",
                error_code="unknown_result",
                error_message=(
                    "unexpected provider failure; "
                    "the request may already have been processed"
                ),
                generation_hold=self._classified_generation_hold(
                    generation_hold, "ambiguous"
                ),
            )
            raise AIServiceError(
                "unexpected provider failure; "
                "the request may already have been processed"
            ) from exc

    def run_job(
        self,
        job_id: int,
        *,
        force_new_provider_request: bool = False,
        prepared_task: Optional[PreparedTask] = None,
        force_generation_hold: bool = False,
        retry_paid_failure_hold: bool = False,
    ) -> Dict[str, object]:
        job = self.database.ai_job(int(job_id))
        if job is None:
            raise AIInputError("AI job %s was not found" % job_id)
        if job.get("state") == "succeeded" and job.get("artifact_id"):
            artifact = self.database.ai_artifact(int(job["artifact_id"]))
            if artifact is not None:
                return self._artifact_result(artifact, cache_hit=True)
        if job.get("state") in {"unknown", "permanent_failed", "cancelled"}:
            raise AIServiceError(
                "AI job %s cannot run from state %s" % (job_id, job.get("state"))
            )
        next_attempt_at = str(job.get("next_attempt_at") or "")
        if (
            job.get("state") == "retryable"
            and next_attempt_at
            and next_attempt_at > _iso(self._now())
        ):
            raise AIServiceError(
                "AI job %s is in provider backoff until %s"
                % (job_id, next_attempt_at)
            )
        try:
            if prepared_task is None:
                prepared = self._prepare_from_job(
                    job,
                    fetch_if_missing=bool(
                        force_new_provider_request
                        and str(job.get("input_scope") or "") == "full_text"
                        and self.config.input_policy == "fetch_on_demand_ephemeral"
                    ),
                )
            else:
                try:
                    request = json.loads(str(job["request_json"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AIInputError("AI job request is invalid") from exc
                if (
                    not isinstance(request, dict)
                    or prepared_task.artifact_key != request.get("expected_artifact_key")
                    or prepared_task.input_hash != request.get("expected_input_hash")
                    or prepared_task.artifact_key != job.get("artifact_key")
                ):
                    raise AIInputError("prepared AI input does not match the queued job")
                prepared = prepared_task
                if self._is_stale(prepared):
                    raise AIInputError("article input changed before the AI request was sent")
        except AIInputError as exc:
            try:
                self.database.cancel_ai_job(int(job_id), "input_changed", str(exc))
            except AIJobConflict:
                pass
            raise
        generation_hold = self._generation_hold_template(
            prepared,
            workload_kind="article",
        )
        (
            observed_generation_holds,
            preflight_generation_hold_revision,
        ) = self._generation_hold_preflight(
            generation_hold,
            force_held=force_generation_hold,
            retry_paid_failure=retry_paid_failure_hold,
        )
        exact_generation_hold = self._exact_observed_generation_hold(
            generation_hold,
            observed_generation_holds,
        )
        preflight_generation_hold_snapshot = (
            exact_generation_hold.delete_guard()
            if exact_generation_hold is not None
            else None
        )
        settle_definitive_failure = (
            self._definitive_failure_may_settle_provisional(
                exact_generation_hold,
                observed_generation_holds,
                retry_paid_failure=(
                    retry_paid_failure_hold or force_generation_hold
                ),
            )
        )
        try:
            self._require_enabled(prepared.task_type, prepared.input_scope)
            self._ensure_api_key()
            estimated_input = conservative_token_estimate(
                prepared.definition.instructions,
                prepared.input_text,
                canonical_json(prepared.definition.schema),
            )
            reserved_cost, price_snapshot = self._price_reservation(
                prepared, estimated_input
            )
            daily_start, monthly_start, daily_reset, monthly_reset = self._budget_window()
            next_attempt = int(job.get("attempt_count") or 0) + 1
            idempotency_key = self._provider_idempotency_key(
                job_id=int(job_id),
                next_attempt=next_attempt,
                artifact_key=prepared.artifact_key,
            )
        except (AIServiceError, ProviderConfigError) as exc:
            code = "missing_api_key" if isinstance(exc, ProviderConfigError) else "preflight_failed"
            try:
                self.database.cancel_ai_job(int(job_id), code, str(exc))
            except AIJobConflict:
                pass
            raise
        budget = self.config.budget
        reservation = self.database.reserve_ai_attempt(
            job_id=int(job_id),
            idempotency_key=idempotency_key,
            requested_provider=self.config.provider,
            requested_model=prepared.model,
            estimated_input_tokens=estimated_input,
            reserved_output_tokens=prepared.max_output_tokens,
            reserved_cost_micros=reserved_cost,
            price_snapshot=price_snapshot,
            daily_started_at=daily_start,
            monthly_started_at=monthly_start,
            daily_reset_at=daily_reset,
            monthly_reset_at=monthly_reset,
            daily_max_requests=budget.daily_max_requests,
            daily_max_total_tokens=budget.daily_max_total_tokens,
            daily_max_cost_micros=int(round(budget.daily_max_cost_usd * 1_000_000)),
            monthly_max_requests=budget.monthly_max_requests,
            monthly_max_total_tokens=budget.monthly_max_total_tokens,
            monthly_max_cost_micros=int(round(budget.monthly_max_cost_usd * 1_000_000)),
        )
        if reservation.get("cache_hit"):
            return self._artifact_result(reservation, cache_hit=True)
        attempt_id = int(reservation["id"])
        provider_request = ProviderRequest(
            model=prepared.model,
            instructions=prepared.definition.instructions,
            input_text=prepared.input_text,
            json_schema=prepared.definition.schema,
            schema_name=prepared.definition.schema_name,
            idempotency_key=idempotency_key,
            max_output_tokens=prepared.max_output_tokens,
            reasoning_effort=self.config.reasoning_effort,
        )
        response, provisional_generation_hold = self._call_provider_for_attempt(
            attempt_id=attempt_id,
            prepared=prepared,
            provider_request=provider_request,
            generation_hold=generation_hold,
            preflight_generation_hold_snapshot=(
                preflight_generation_hold_snapshot
            ),
            preflight_generation_hold_revision=(
                preflight_generation_hold_revision
            ),
            settle_definitive_failure=settle_definitive_failure,
        )

        usage_dict = self._usage_dict(response.usage)
        usage_confirmed = bool(
            response.usage_reported and usage_dict.get("total_tokens", 0) > 0
        )
        actual_cost = (
            self._actual_cost(
                self.config.prices.get(prepared.model), response.usage
            )
            if usage_confirmed
            else None
        )
        try:
            validated, readable = parse_and_validate_output(
                prepared.task_type,
                response.output_text,
                target_language=prepared.target_language,
                input_scope=prepared.input_scope,
                translated_fields=prepared.translated_fields,
                translation_input=(
                    prepared.input_payload
                    if prepared.task_type == "translation"
                    else None
                ),
            )
        except ValueError as exc:
            current = self.database.ai_job(int(job_id)) or job
            fallback_eligible = bool(
                usage_confirmed and self._automatic_fallback_enabled()
            )
            retryable = (
                usage_confirmed
                and not fallback_eligible
                and not (
                    prepared.input_scope == "full_text"
                    and self.config.input_policy == "fetch_on_demand_ephemeral"
                )
                and int(current.get("attempt_count") or 0)
                < int(current.get("max_attempts") or 1)
            )
            self.database.fail_ai_attempt(
                attempt_id=attempt_id,
                job_state="retryable" if retryable else "permanent_failed",
                error_class="output_validation",
                error_code="invalid_structured_output",
                error_message=str(exc),
                http_status=200,
                usage=usage_dict if usage_confirmed else None,
                actual_cost_micros=actual_cost,
                next_attempt_at=_iso(self._now() + timedelta(seconds=5)) if retryable else None,
                preserve_reservation=not usage_confirmed,
                generation_hold=self._failure_generation_hold(
                    generation_hold,
                    "paid_failure" if usage_confirmed else "ambiguous",
                    fallback_eligible=fallback_eligible,
                ),
                settle_provisional_generation_hold=(
                    provisional_generation_hold
                    if usage_confirmed and settle_definitive_failure
                    else None
                ),
            )
            message = "provider output failed local validation: %s" % exc
            error = (
                self._fallback_error(
                    message,
                    reason_code="invalid_structured_output",
                    provider_call_made=True,
                    generation_hold_key=str(
                        generation_hold.get("hold_key") or ""
                    ),
                )
                if fallback_eligible
                else AIServiceError(message)
            )
            raise error from exc

        artifact = self._provider_artifact(
            prepared,
            validated=validated,
            readable=readable,
            response=response,
            usage_confirmed=usage_confirmed,
        )
        stored = self.database.complete_ai_attempt(
            attempt_id=attempt_id,
            artifact=artifact,
            usage=usage_dict,
            actual_cost_micros=actual_cost,
            usage_confirmed=usage_confirmed,
            clear_generation_hold_snapshots=(
                (provisional_generation_hold,)
                + self._generation_holds_cleared_after_success(
                    prepared,
                    observed_generation_holds,
                )
            ),
        )
        return self._artifact_result(stored, cache_hit=False)

    def _provider_artifact(
        self,
        prepared: PreparedTask,
        *,
        validated: Mapping[str, object],
        readable: str,
        response: ProviderResponse,
        usage_confirmed: bool,
        response_text: Optional[str] = None,
    ) -> Dict[str, object]:
        output_json = canonical_json(validated)
        raw_response = response.output_text if response_text is None else response_text
        return {
            "article_id": prepared.article_id,
            "task_type": prepared.task_type,
            "input_scope": prepared.input_scope,
            "source_language": "unknown",
            "target_language": prepared.target_language,
            "artifact_key": prepared.artifact_key,
            "input_hash": prepared.input_hash,
            "article_content_hash": prepared.article_content_hash,
            "source_artifact_id": None,
            "content_snapshot_id": prepared.content_snapshot_id,
            "prompt_version": prepared.definition.prompt_version,
            "prompt_hash": prepared.definition.prompt_hash,
            "response_schema_version": prepared.definition.schema_version,
            "response_schema_hash": prepared.definition.schema_hash,
            "provider": self.config.provider,
            "requested_model": prepared.model,
            "resolved_model": response.model,
            "generation_params_hash": prepared.generation_params_hash,
            "provider_response_id": response.request_id or response.response_id,
            "output_json": output_json,
            "output_text": readable,
            "output_hash": stable_hash(output_json),
            "status": "stale" if self._is_stale(prepared) else "succeeded",
            "input_truncated": int(prepared.input_truncated),
            "http_status": 200,
            "finish_reason": (
                "completed" if usage_confirmed else "completed_usage_unreported"
            ),
            "response_hash": hashlib.sha256(
                raw_response.encode("utf-8")
            ).hexdigest(),
        }

    def _is_stale(self, prepared: PreparedTask) -> bool:
        if prepared.article_id is not None:
            current = self.database.article(prepared.article_id)
            return current is None or str(current.get("content_hash") or "") != prepared.article_content_hash
        current_articles: List[Mapping[str, object]] = []
        for article_id in prepared.expected_article_ids:
            current = self.database.article(article_id)
            if current is None:
                return True
            current_articles.append(current)
        current_hash = stable_hash(
            [
                {"id": int(article["id"]), "content_hash": str(article.get("content_hash") or "")}
                for article in current_articles
            ]
        )
        return current_hash != prepared.article_content_hash

    @staticmethod
    def _usage_dict(usage: ProviderUsage) -> Dict[str, int]:
        computed_total = int(usage.input_tokens) + int(usage.output_tokens)
        return {
            "input_tokens": int(usage.input_tokens),
            "cached_input_tokens": int(usage.cached_input_tokens),
            "cache_write_tokens": int(usage.cache_write_input_tokens),
            "output_tokens": int(usage.output_tokens),
            "reasoning_tokens": int(usage.reasoning_tokens),
            "total_tokens": max(int(usage.total_tokens), computed_total),
        }

    @staticmethod
    def _artifact_result(
        artifact: Mapping[str, object], *, cache_hit: bool
    ) -> Dict[str, object]:
        result = dict(artifact)
        try:
            result["output"] = json.loads(str(result.get("output_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["output"] = {}
        result["cache_hit"] = bool(cache_hit)
        return result

    def enqueue_batch(
        self,
        articles: Sequence[Mapping[str, object]],
        *,
        task_type: str,
        target_language: str = "zh-CN",
        input_scope: str = "metadata",
        confirmed: bool = False,
    ) -> List[Dict[str, object]]:
        if not confirmed:
            raise AIInputError("batch enqueue requires explicit confirmation")
        if not self.config.batch.enabled:
            raise AIFeatureDisabledError("AI batch processing is disabled in configuration")
        scope = self._normalize_scope(input_scope)
        self._require_enabled(task_type, scope)
        if (
            scope == "full_text"
            and self.config.input_policy == "fetch_on_demand_ephemeral"
        ):
            raise AIFeatureDisabledError(
                "ephemeral full text cannot be queued; use an immediate command or cached-local policy"
            )
        limited = list(articles[: self.config.batch.max_articles_per_run])
        jobs = []
        for article in limited:
            prepared = self.prepare_article(
                int(article["id"]),
                task_type=task_type,
                target_language=target_language,
                input_scope=scope,
                fetch_if_missing=scope == "full_text",
            )
            jobs.append(self.enqueue(prepared, priority=100, trigger_kind="batch"))
        return jobs

    def run_worker(self, *, limit: Optional[int] = None) -> List[Dict[str, object]]:
        if not self.config.enabled:
            raise AIDisabledError("AI is disabled")
        maximum = max(1, min(int(limit or self.config.batch.max_articles_per_run), 1000))
        self.database.recover_stalled_ai_jobs()
        worker_id = "worker-%s" % uuid.uuid4().hex
        results: List[Dict[str, object]] = []
        for _ in range(maximum):
            job = self.database.lease_ai_job(worker_id)
            if job is None:
                break
            try:
                artifact = self.run_job(int(job["id"]))
                results.append(
                    {"job_id": int(job["id"]), "state": "succeeded", "artifact": artifact}
                )
            except Exception as exc:
                current = self.database.ai_job(int(job["id"])) or job
                results.append(
                    {
                        "job_id": int(job["id"]),
                        "state": str(current.get("state") or "failed"),
                        "error": str(exc)[:500],
                    }
                )
        return results

    def retry_job(self, job_id: int, *, allow_unknown: bool = False) -> Dict[str, object]:
        """Explicitly retry a terminal job, including a potentially billed unknown."""

        if not self.config.enabled:
            raise AIDisabledError("AI is disabled")
        self._ensure_api_key()
        job = self.database.requeue_ai_job(
            int(job_id), allow_unknown=allow_unknown
        )
        return self.run_job(
            int(job["id"]),
            force_new_provider_request=True,
            force_generation_hold=True,
        )

    def status(self) -> Dict[str, object]:
        daily_start, monthly_start = self._budget_starts()
        result = self.database.ai_status(daily_start, monthly_start)
        result.update(
            {
                "enabled": self.config.enabled,
                "provider": self.config.provider,
                "api_key_present": bool(
                    str(os.environ.get(self.config.api_key_environment, "")).strip()
                ),
                "input_policy": self.config.input_policy,
                "budget_timezone": self.config.budget.timezone,
                "models": {
                    "translation": self.config.translation_model,
                    "summary": self.config.summary_model,
                },
                "limits": {
                    "daily_max_requests": self.config.budget.daily_max_requests,
                    "daily_max_total_tokens": self.config.budget.daily_max_total_tokens,
                    "monthly_max_requests": self.config.budget.monthly_max_requests,
                    "monthly_max_total_tokens": self.config.budget.monthly_max_total_tokens,
                },
            }
        )
        return result

    def audit(self, limit: int = 100) -> List[Dict[str, object]]:
        return self.database.list_ai_attempts(limit)
