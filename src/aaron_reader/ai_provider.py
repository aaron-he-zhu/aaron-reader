"""Fixed Chat Completions transports for Aaron Reader cloud runs.

This module is deliberately isolated from the deterministic sync path.  It
uses only the Python standard library, performs one request per ``generate``
call, and never retries a request internally: a caller must make the explicit
decision to risk another billable request after a failure.
"""

import json
import math
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional

from .ai_profiles import DEEPSEEK_PROFILE, OPENROUTER_PROFILE


DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = DEEPSEEK_PROFILE.model
DEEPSEEK_API_KEY_ENVIRONMENT = DEEPSEEK_PROFILE.api_key_environment
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = OPENROUTER_PROFILE.model
OPENROUTER_API_KEY_ENVIRONMENT = OPENROUTER_PROFILE.api_key_environment
OPENROUTER_SITE_URL = "https://aaron-reader.aaron-he-zhu.workers.dev/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ERROR_TEXT_LIMIT = 240
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")
_TOKEN_FIELD = re.compile(r"^[A-Za-z0-9_-]+$")
_OPENROUTER_ERROR_TYPES = frozenset(
    {
        "authentication",
        "content_policy_violation",
        "context_length_exceeded",
        "image_download_failed",
        "image_not_found",
        "image_too_large",
        "image_too_small",
        "invalid_image",
        "invalid_prompt",
        "invalid_request",
        "max_tokens_exceeded",
        "not_found",
        "payload_too_large",
        "payment_required",
        "permission_denied",
        "precondition_failed",
        "provider_overloaded",
        "provider_unavailable",
        "rate_limit_exceeded",
        "refusal",
        "server",
        "string_too_long",
        "timeout",
        "token_limit_exceeded",
        "unmapped",
        "unprocessable",
        "unsupported_image_format",
    }
)
_POLICY_FAILURE_MARKERS = (
    "refusal",
    "content_filter",
    "content-filter",
    "content_policy",
    "content-policy",
    "safety",
    "moderation",
    "policy",
    "abuse",
    "blocked",
)


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class ProviderConfigError(ProviderError):
    """The request cannot be sent because local configuration is invalid."""


class ProviderHTTPError(ProviderError):
    """The provider returned a non-success HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after: Optional[float] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.retry_after = retry_after
        self.retryable = bool(retryable)


class ProviderUnknownError(ProviderError):
    """A request may have been processed, so automatic retry is unsafe."""


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    @property
    def cached_tokens(self) -> int:
        """Compatibility name for provider-reported cached input tokens."""

        return self.cached_input_tokens

    @property
    def cache_write_tokens(self) -> int:
        """Compatibility name for provider-reported cache-write input tokens."""

        return self.cache_write_input_tokens


class ProviderKnownError(ProviderError):
    """A billed response failed, but exact usage and completion are known."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        usage: ProviderUsage,
        model: str,
        request_id: str,
        response_id: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.usage = usage
        self.model = model
        self.request_id = request_id
        self.response_id = response_id


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    instructions: str
    input_text: str
    json_schema: Mapping[str, object]
    idempotency_key: str
    max_output_tokens: int
    reasoning_effort: str = "none"
    schema_name: str = "aaron_reader_result"


@dataclass(frozen=True)
class ProviderResponse:
    output_text: str
    usage: ProviderUsage
    model: str
    request_id: str
    response_id: str = ""
    usage_reported: bool = True


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


class _ProviderTransportBase:
    """Shared validation and bounded standard-library HTTP plumbing."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        opener: Optional[object] = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ProviderConfigError("provider timeout must be a positive number")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1_024
            or max_response_bytes > MAX_RESPONSE_BYTES
        ):
            raise ProviderConfigError(
                "provider max_response_bytes must be between 1024 and %d"
                % MAX_RESPONSE_BYTES
            )
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def _open(self, request: urllib.request.Request):
        open_method = getattr(self._opener, "open", None)
        if callable(open_method):
            return open_method(request, timeout=self.timeout_seconds)
        if callable(self._opener):
            return self._opener(request, timeout=self.timeout_seconds)
        raise ProviderConfigError("provider opener is not callable")

    @staticmethod
    def _validate_request(request: ProviderRequest) -> None:
        for label, value in (
            ("model", request.model),
            ("instructions", request.instructions),
            ("input_text", request.input_text),
            ("reasoning_effort", request.reasoning_effort),
            ("schema_name", request.schema_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ProviderConfigError("%s must be a non-empty string" % label)
            if "\x00" in value:
                raise ProviderConfigError("%s contains a NUL character" % label)
        if not isinstance(request.json_schema, Mapping):
            raise ProviderConfigError("json_schema must be an object")
        if (
            isinstance(request.max_output_tokens, bool)
            or not isinstance(request.max_output_tokens, int)
            or request.max_output_tokens <= 0
        ):
            raise ProviderConfigError("max_output_tokens must be a positive integer")
        if (
            not isinstance(request.idempotency_key, str)
            or not request.idempotency_key
            or len(request.idempotency_key) > 200
            or not _IDEMPOTENCY_KEY.fullmatch(request.idempotency_key)
        ):
            raise ProviderConfigError(
                "idempotency_key must contain 1-200 letters, digits, '.', '_', ':', or '-'"
            )
        if len(request.schema_name) > 64 or not _TOKEN_FIELD.fullmatch(
            request.schema_name
        ):
            raise ProviderConfigError(
                "schema_name must contain 1-64 letters, digits, '_' or '-'"
            )
        if len(request.reasoning_effort) > 32 or not _TOKEN_FIELD.fullmatch(
            request.reasoning_effort
        ):
            raise ProviderConfigError("reasoning_effort contains invalid characters")

    @staticmethod
    def _http_error(
        status: int,
        headers: object,
        reason: object,
        api_key: str,
    ) -> ProviderHTTPError:
        retry_after = _parse_retry_after(_header_value(headers, "Retry-After"))
        retryable = status in (408, 409, 425, 429) or 500 <= status <= 599
        detail = _safe_error_text(reason, secret=api_key)
        message = "provider returned HTTP %d" % status
        if detail:
            message += ": %s" % detail
        return ProviderHTTPError(
            message,
            status=status,
            retry_after=retry_after,
            retryable=retryable,
        )


class _FixedChatCompletionsProvider(_ProviderTransportBase):
    """Shared one-shot transport for a closed provider/model profile."""

    chat_completions_url = ""
    model = ""
    api_key_environment = ""
    provider_label = "AI provider"
    user_agent = "AaronReader/1.1 (cloud AI automation)"

    @staticmethod
    def _typed_error_code(value: object) -> str:
        """Return only OpenRouter's bounded, documented error vocabulary.

        Provider messages and metadata can contain prompt excerpts or other
        sensitive detail, so only the stable ``error_type`` token is retained.
        Unknown or malformed envelopes deliberately collapse to the generic
        non-fallback ``provider_error`` code.
        """

        if not isinstance(value, Mapping):
            return "provider_error"
        metadata = value.get("metadata")
        raw_type = (
            metadata.get("error_type")
            if isinstance(metadata, Mapping)
            else None
        )
        if not isinstance(raw_type, str):
            return "provider_error"
        normalized = raw_type.strip().lower()
        if not _TOKEN_FIELD.fullmatch(normalized):
            return "provider_error"
        if normalized not in _OPENROUTER_ERROR_TYPES:
            return "provider_error"
        return normalized

    @staticmethod
    def _policy_response_code(payload: Mapping[str, object]) -> str:
        """Best-effort policy scan that runs before structural validation."""

        top_level_error = payload.get("error")
        if top_level_error not in (None, "", {}, []):
            typed = _FixedChatCompletionsProvider._typed_error_code(
                top_level_error
            )
            if any(marker in typed for marker in _POLICY_FAILURE_MARKERS):
                return typed
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return ""
        for raw_choice in choices[:8]:
            if not isinstance(raw_choice, Mapping):
                continue
            finish_reason = str(raw_choice.get("finish_reason") or "").lower()
            if any(marker in finish_reason for marker in _POLICY_FAILURE_MARKERS):
                return "content_filter"
            raw_message = raw_choice.get("message")
            if isinstance(raw_message, Mapping):
                refusal = raw_message.get("refusal")
                if refusal is not None and (
                    not isinstance(refusal, str) or refusal.strip()
                ):
                    return "refusal"
            raw_error = raw_choice.get("error")
            if raw_error not in (None, "", {}, []):
                typed = _FixedChatCompletionsProvider._typed_error_code(
                    raw_error
                )
                if any(marker in typed for marker in _POLICY_FAILURE_MARKERS):
                    return typed
        return ""

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise ProviderConfigError("request must be a ProviderRequest")
        api_key = str(os.environ.get(self.api_key_environment, "")).strip()
        if not api_key:
            raise ProviderConfigError("%s is not set" % self.api_key_environment)
        if any(ord(character) < 32 or ord(character) == 127 for character in api_key):
            raise ProviderConfigError(
                "%s contains invalid characters" % self.api_key_environment
            )
        self._validate_request(request)
        if request.model.strip() != self.model:
            raise ProviderConfigError(
                "%s automation requires model %s"
                % (self.provider_label, self.model)
            )

        try:
            payload = self._request_payload(request)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderConfigError("json_schema is not JSON serializable") from exc

        http_request = urllib.request.Request(
            self.chat_completions_url,
            data=encoded,
            headers=self._request_headers(api_key),
            method="POST",
        )

        try:
            response = self._open(http_request)
            with response:
                status = int(response.getcode())
                if status < 200 or status >= 300:
                    raise self._http_error(
                        status,
                        getattr(response, "headers", {}),
                        getattr(response, "reason", ""),
                        api_key,
                    )
                content_length = _header_value(
                    getattr(response, "headers", {}), "Content-Length"
                )
                if content_length:
                    try:
                        if int(content_length) > self.max_response_bytes:
                            raise ProviderUnknownError(
                                "provider response exceeded the configured byte limit; "
                                "the request may already have been processed"
                            )
                    except ValueError:
                        pass
                body = response.read(self.max_response_bytes + 1)
                if len(body) > self.max_response_bytes:
                    raise ProviderUnknownError(
                        "provider response exceeded the configured byte limit; "
                        "the request may already have been processed"
                    )
                headers = getattr(response, "headers", {})
        except ProviderError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                provider_error = self._http_error(
                    int(exc.code), exc.headers or {}, exc.reason, api_key
                )
            finally:
                if getattr(exc, "fp", None) is not None:
                    try:
                        exc.close()
                    except Exception:
                        pass
            raise provider_error from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            detail = _safe_error_text(exc, secret=api_key)
            suffix = ": %s" % detail if detail else ""
            raise ProviderUnknownError(
                "provider transport failed%s; the request may already have been processed"
                % suffix
            ) from None
        except Exception as exc:
            detail = _safe_error_text(exc, secret=api_key)
            suffix = ": %s" % detail if detail else ""
            raise ProviderUnknownError(
                "provider response failed%s; the request may already have been processed"
                % suffix
            ) from None

        try:
            decoded = body.decode("utf-8")
            response_payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ProviderUnknownError(
                "provider returned an invalid JSON response; "
                "the request may already have been processed"
            ) from None
        if not isinstance(response_payload, dict):
            raise ProviderUnknownError(
                "provider returned an invalid response object; "
                "the request may already have been processed"
            )

        response_id = _safe_identifier(response_payload.get("id"))
        request_id = _safe_identifier(
            _header_value(headers, "x-request-id")
            or _header_value(headers, "request-id")
        ) or response_id
        resolved_model = _safe_identifier(response_payload.get("model"))
        raw_usage = response_payload.get("usage")
        usage = self._parse_usage(raw_usage)
        usage_reported = self._usage_is_complete(raw_usage, usage)

        raw_provider_error = response_payload.get("error")
        policy_failure = self._policy_response_code(response_payload)
        choices = response_payload.get("choices")
        raw_choice_errors = []
        if isinstance(choices, list):
            raw_choice_errors = [
                choice.get("error")
                for choice in choices[:8]
                if isinstance(choice, Mapping)
                and choice.get("error") not in (None, "", {}, [])
            ]

        error_codes = []
        if raw_provider_error not in (None, "", {}, []):
            # Successful Chat Completions use choice-level terminal errors.
            # A top-level error in a HTTP-200 non-streaming response remains
            # default-deny unless it was a policy signal handled above.
            error_codes.append("provider_error")
        if raw_choice_errors:
            valid_choice_error_envelope = bool(
                isinstance(choices, list)
                and len(choices) == 1
                and isinstance(choices[0], Mapping)
                and choices[0].get("index") in (None, 0)
                and choices[0].get("finish_reason") == "error"
                and isinstance(choices[0].get("error"), Mapping)
            )
            error_codes.extend(
                [
                    self._typed_error_code(raw_error)
                    if valid_choice_error_envelope
                    else "provider_error"
                    for raw_error in raw_choice_errors
                ]
            )
        failure_code = policy_failure
        if not failure_code:
            if error_codes:
                # A generic/unknown error or conflicting typed signals wins
                # over availability.  Mixed envelopes remain fail-closed.
                failure_code = (
                    error_codes[0]
                    if len(set(error_codes)) == 1
                    else "provider_error"
                )
                if "provider_error" in error_codes:
                    failure_code = "provider_error"
            elif not response_id:
                failure_code = "response_id"
            elif not self._resolved_model_is_valid(resolved_model):
                failure_code = "response_model"
            elif response_payload.get("object") != "chat.completion":
                failure_code = "response_object"
        choice: Mapping[str, Any] = {}
        if not failure_code:
            if not isinstance(choices, list) or len(choices) != 1:
                failure_code = "response_choices"
            elif not isinstance(choices[0], Mapping) or choices[0].get("index") != 0:
                failure_code = "response_choice"
            else:
                choice = choices[0]

        message: Mapping[str, Any] = {}
        finish_reason = ""
        if not failure_code:
            finish_reason = str(choice.get("finish_reason") or "")
            if finish_reason != "stop":
                failure_code = (
                    "finish_%s" % finish_reason
                    if finish_reason
                    else "finish_reason"
                )
            raw_message = choice.get("message")
            if not isinstance(raw_message, Mapping):
                failure_code = failure_code or "response_message"
            else:
                message = raw_message
        if not failure_code and message.get("role") != "assistant":
            failure_code = "response_role"
        if not failure_code and message.get("tool_calls") not in (None, []):
            failure_code = "tool_calls"
        refusal = message.get("refusal")
        if refusal is not None and (
            not isinstance(refusal, str) or refusal.strip()
        ):
            # A provider refusal is a policy boundary, not an ordinary quality
            # failure.  It must take precedence even when finish_reason also
            # reports length, error, or another non-stop disposition so the
            # fallback policy cannot route around the refusal.
            failure_code = "refusal"
        reasoning_content = message.get("reasoning_content")
        if not failure_code and isinstance(reasoning_content, str) and reasoning_content.strip():
            failure_code = "thinking_output"
        reasoning = message.get("reasoning")
        if not failure_code and isinstance(reasoning, str) and reasoning.strip():
            failure_code = "thinking_output"
        reasoning_details = message.get("reasoning_details")
        if not failure_code and reasoning_details not in (None, [], ""):
            failure_code = "thinking_output"
        if not failure_code and usage.reasoning_tokens:
            failure_code = "thinking_tokens"
        output_text = message.get("content") if message else None
        if not failure_code and (not isinstance(output_text, str) or not output_text.strip()):
            failure_code = "no_output"

        if failure_code:
            if usage_reported:
                raise ProviderKnownError(
                    "provider returned a known unusable chat completion",
                    code=failure_code[:100],
                    usage=usage,
                    model=resolved_model,
                    request_id=request_id,
                    response_id=response_id,
                )
            raise ProviderUnknownError(
                "provider response was unusable and usage was unavailable; "
                "the request may already have been processed"
            )

        return ProviderResponse(
            output_text=str(output_text).strip(),
            usage=usage,
            model=resolved_model,
            request_id=request_id,
            response_id=response_id,
            usage_reported=usage_reported,
        )

    def _request_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _resolved_model_is_valid(self, resolved_model: str) -> bool:
        return resolved_model == self.model

    def _parse_usage(self, value: object) -> ProviderUsage:
        del value
        raise NotImplementedError

    def _usage_is_complete(
        self,
        value: object,
        parsed: ProviderUsage,
    ) -> bool:
        del value, parsed
        raise NotImplementedError

    def _request_payload(self, request: ProviderRequest) -> Dict[str, object]:
        del request
        raise NotImplementedError


class DeepSeekChatCompletionsProvider(_FixedChatCompletionsProvider):
    """One-shot client for Aaron Reader's fixed DeepSeek JSON-mode profile."""

    chat_completions_url = DEEPSEEK_CHAT_COMPLETIONS_URL
    model = DEEPSEEK_MODEL
    api_key_environment = DEEPSEEK_API_KEY_ENVIRONMENT
    provider_label = "DeepSeek"
    user_agent = "AaronReader/1.1 (DeepSeek cloud automation)"

    def _parse_usage(self, value: object) -> ProviderUsage:
        return _parse_deepseek_usage(value)

    def _usage_is_complete(
        self,
        value: object,
        parsed: ProviderUsage,
    ) -> bool:
        return _deepseek_usage_is_complete(value, parsed)

    def _request_payload(self, request: ProviderRequest) -> Dict[str, object]:
        schema_json = json.dumps(
            dict(request.json_schema),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instructions = (
            "%s\n\nReturn JSON only. The JSON object must match this schema "
            "exactly; do not add Markdown or commentary.\nJSON Schema: %s"
            % (request.instructions.strip(), schema_json)
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": request.input_text},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }


class OpenRouterChatCompletionsProvider(_FixedChatCompletionsProvider):
    """One-shot client for the fixed OpenRouter free-model router profile."""

    chat_completions_url = OPENROUTER_CHAT_COMPLETIONS_URL
    model = OPENROUTER_MODEL
    api_key_environment = OPENROUTER_API_KEY_ENVIRONMENT
    provider_label = "OpenRouter"
    user_agent = "AaronReader/1.1 (OpenRouter free cloud automation)"

    def _request_headers(self, api_key: str) -> Dict[str, str]:
        headers = super()._request_headers(api_key)
        headers.update(
            {
                "HTTP-Referer": OPENROUTER_SITE_URL,
                "X-OpenRouter-Title": "Aaron Reader",
            }
        )
        return headers

    def _resolved_model_is_valid(self, resolved_model: str) -> bool:
        # The free router deliberately returns the concrete model it selected,
        # not necessarily the requested ``openrouter/free`` router slug.
        return bool(resolved_model)

    def _parse_usage(self, value: object) -> ProviderUsage:
        return _parse_openrouter_usage(value)

    def _usage_is_complete(
        self,
        value: object,
        parsed: ProviderUsage,
    ) -> bool:
        return _openrouter_usage_is_complete(value, parsed)

    def _request_payload(self, request: ProviderRequest) -> Dict[str, object]:
        schema = dict(request.json_schema)
        schema_json = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instructions = (
            "%s\n\nReturn JSON only. The JSON object must match this schema "
            "exactly; do not add Markdown or commentary.\nJSON Schema: %s"
            % (request.instructions.strip(), schema_json)
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": request.input_text},
            ],
            "reasoning": {"effort": "none"},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "provider": {"require_parameters": True},
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }


def _parse_deepseek_usage(value: object) -> ProviderUsage:
    usage = value if isinstance(value, Mapping) else {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, Mapping):
        completion_details = {}
    input_tokens = _token_count(usage.get("prompt_tokens"))
    output_tokens = _token_count(usage.get("completion_tokens"))
    total_tokens = _token_count(usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return ProviderUsage(
        input_tokens=input_tokens,
        cached_input_tokens=_token_count(usage.get("prompt_cache_hit_tokens")),
        cache_write_input_tokens=0,
        output_tokens=output_tokens,
        reasoning_tokens=_token_count(completion_details.get("reasoning_tokens")),
        total_tokens=total_tokens,
    )


def _deepseek_usage_is_complete(value: object, parsed: ProviderUsage) -> bool:
    """Validate the current DeepSeek non-streaming usage contract exactly."""

    if not isinstance(value, Mapping):
        return False
    counts = {}
    for key in (
        "prompt_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "completion_tokens",
        "total_tokens",
    ):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
        counts[key] = raw
    details = value.get("completion_tokens_details")
    if details is not None and not isinstance(details, Mapping):
        return False
    reasoning = 0
    if isinstance(details, Mapping) and "reasoning_tokens" in details:
        raw_reasoning = details.get("reasoning_tokens")
        if (
            isinstance(raw_reasoning, bool)
            or not isinstance(raw_reasoning, int)
            or raw_reasoning < 0
        ):
            return False
        reasoning = raw_reasoning
    return bool(
        counts["prompt_tokens"] > 0
        and counts["prompt_tokens"]
        == counts["prompt_cache_hit_tokens"] + counts["prompt_cache_miss_tokens"]
        and counts["total_tokens"]
        == counts["prompt_tokens"] + counts["completion_tokens"]
        and reasoning <= counts["completion_tokens"]
        and parsed.input_tokens == counts["prompt_tokens"]
        and parsed.cached_input_tokens == counts["prompt_cache_hit_tokens"]
        and parsed.output_tokens == counts["completion_tokens"]
        and parsed.reasoning_tokens == reasoning
        and parsed.total_tokens == counts["total_tokens"]
    )


def _parse_openrouter_usage(value: object) -> ProviderUsage:
    usage = value if isinstance(value, Mapping) else {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, Mapping):
        completion_details = {}
    input_tokens = _token_count(usage.get("prompt_tokens"))
    output_tokens = _token_count(usage.get("completion_tokens"))
    total_tokens = _token_count(usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return ProviderUsage(
        input_tokens=input_tokens,
        cached_input_tokens=_token_count(prompt_details.get("cached_tokens")),
        cache_write_input_tokens=_token_count(
            prompt_details.get("cache_write_tokens")
        ),
        output_tokens=output_tokens,
        reasoning_tokens=_token_count(
            completion_details.get("reasoning_tokens")
        ),
        total_tokens=total_tokens,
    )


def _openrouter_usage_is_complete(value: object, parsed: ProviderUsage) -> bool:
    """Validate OpenRouter's normalized non-streaming usage contract."""

    if not isinstance(value, Mapping):
        return False
    counts = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
        counts[key] = raw

    prompt_details = value.get("prompt_tokens_details")
    if prompt_details is not None and not isinstance(prompt_details, Mapping):
        return False
    cached_tokens = 0
    cache_write_tokens = 0
    if isinstance(prompt_details, Mapping):
        for key in ("cached_tokens", "cache_write_tokens"):
            if key not in prompt_details:
                continue
            raw = prompt_details.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                return False
            if key == "cached_tokens":
                cached_tokens = raw
            else:
                cache_write_tokens = raw

    completion_details = value.get("completion_tokens_details")
    if completion_details is not None and not isinstance(
        completion_details, Mapping
    ):
        return False
    reasoning_tokens = 0
    if (
        isinstance(completion_details, Mapping)
        and "reasoning_tokens" in completion_details
    ):
        raw_reasoning = completion_details.get("reasoning_tokens")
        if (
            isinstance(raw_reasoning, bool)
            or not isinstance(raw_reasoning, int)
            or raw_reasoning < 0
        ):
            return False
        reasoning_tokens = raw_reasoning

    return bool(
        counts["prompt_tokens"] > 0
        and counts["total_tokens"]
        == counts["prompt_tokens"] + counts["completion_tokens"]
        and cached_tokens <= counts["prompt_tokens"]
        and cache_write_tokens <= counts["prompt_tokens"]
        and cached_tokens + cache_write_tokens <= counts["prompt_tokens"]
        and reasoning_tokens <= counts["completion_tokens"]
        and parsed.input_tokens == counts["prompt_tokens"]
        and parsed.cached_input_tokens == cached_tokens
        and parsed.cache_write_input_tokens == cache_write_tokens
        and parsed.output_tokens == counts["completion_tokens"]
        and parsed.reasoning_tokens == reasoning_tokens
        and parsed.total_tokens == counts["total_tokens"]
    )


def _token_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _header_value(headers: object, name: str) -> str:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name, "")
    if not value:
        items = getattr(headers, "items", None)
        if callable(items):
            lowered = name.lower()
            for key, candidate in items():
                if str(key).lower() == lowered:
                    value = candidate
                    break
    return str(value or "").strip()


def _parse_retry_after(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned.strip()[:200]


def _safe_error_text(value: object, *, secret: str = "") -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "[redacted]")
    text = " ".join(text.replace("\x00", " ").split())
    if len(text) > _ERROR_TEXT_LIMIT:
        text = text[: _ERROR_TEXT_LIMIT - 1].rstrip() + "…"
    return text


__all__ = [
    "DEEPSEEK_API_KEY_ENVIRONMENT",
    "DEEPSEEK_CHAT_COMPLETIONS_URL",
    "DEEPSEEK_MODEL",
    "DeepSeekChatCompletionsProvider",
    "MAX_RESPONSE_BYTES",
    "OPENROUTER_API_KEY_ENVIRONMENT",
    "OPENROUTER_CHAT_COMPLETIONS_URL",
    "OPENROUTER_MODEL",
    "OpenRouterChatCompletionsProvider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderKnownError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderUnknownError",
    "ProviderUsage",
]
