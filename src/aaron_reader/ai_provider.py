"""Opt-in OpenAI Responses API transport for Aaron Reader.

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


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ERROR_TEXT_LIMIT = 240
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")
_TOKEN_FIELD = re.compile(r"^[A-Za-z0-9_-]+$")


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
    reasoning_effort: str = "medium"
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


class OpenAIResponsesProvider:
    """A small, injectable client for the fixed OpenAI Responses endpoint."""

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

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """Send exactly one Responses API request and return validated metadata."""

        if not isinstance(request, ProviderRequest):
            raise ProviderConfigError("request must be a ProviderRequest")
        api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
        if not api_key:
            raise ProviderConfigError("OPENAI_API_KEY is not set")
        if any(ord(character) < 32 or ord(character) == 127 for character in api_key):
            raise ProviderConfigError("OPENAI_API_KEY contains invalid characters")
        self._validate_request(request)
        payload = self._request_payload(request)
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderConfigError("json_schema is not JSON serializable") from exc

        http_request = urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=encoded,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json",
                "Idempotency-Key": request.idempotency_key,
                "User-Agent": "AaronReader/1.1 (opt-in AI provider)",
            },
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
        header_request_id = _safe_identifier(
            _header_value(headers, "x-request-id")
            or _header_value(headers, "request-id")
        )
        raw_usage = response_payload.get("usage")
        parsed_usage = _parse_usage(raw_usage)
        usage_reported = _usage_is_complete(raw_usage, parsed_usage)
        resolved_model = _safe_identifier(response_payload.get("model")) or request.model
        request_id = header_request_id or response_id
        status = response_payload.get("status")
        error_present = response_payload.get("error") is not None
        incomplete = response_payload.get("incomplete_details") is not None
        failure_code = ""
        if status != "completed":
            failure_code = "incomplete" if status == "incomplete" else "response_status"
        elif error_present:
            failure_code = "response_error"
        elif incomplete:
            failure_code = "incomplete"
        elif _contains_refusal(response_payload):
            failure_code = "refusal"
        if failure_code:
            if usage_reported:
                raise ProviderKnownError(
                    "provider returned a known non-completed result",
                    code=failure_code,
                    usage=parsed_usage,
                    model=resolved_model,
                    request_id=request_id,
                    response_id=response_id,
                )
            raise ProviderUnknownError(
                "provider response was not completed and usage was unavailable; "
                "the request may already have been processed"
            )

        output_text = _extract_output_text(response_payload)
        if not output_text:
            if usage_reported:
                raise ProviderKnownError(
                    "provider completed without usable output",
                    code="no_output",
                    usage=parsed_usage,
                    model=resolved_model,
                    request_id=request_id,
                    response_id=response_id,
                )
            raise ProviderUnknownError(
                "provider response contained no output_text and usage was unavailable; "
                "the request may already have been processed"
            )
        return ProviderResponse(
            output_text=output_text,
            usage=parsed_usage,
            model=resolved_model,
            request_id=request_id,
            response_id=response_id,
            usage_reported=usage_reported and parsed_usage.output_tokens > 0,
        )

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
    def _request_payload(request: ProviderRequest) -> Dict[str, object]:
        return {
            "model": request.model.strip(),
            "instructions": request.instructions,
            "input": request.input_text,
            "store": False,
            "reasoning": {"effort": request.reasoning_effort.strip()},
            "max_output_tokens": request.max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "strict": True,
                    "schema": dict(request.json_schema),
                }
            },
        }

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


def _extract_output_text(payload: Mapping[str, Any]) -> str:
    pieces = []
    output = payload.get("output")
    if isinstance(output, list):
        for message in output:
            if not isinstance(message, Mapping):
                continue
            nested_message = message.get("message")
            if isinstance(nested_message, Mapping):
                message = nested_message
            if message.get("type") == "output_text":
                text = message.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, Mapping) or item.get("type") != "output_text":
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
    if not pieces and isinstance(payload.get("output_text"), str):
        pieces.append(str(payload["output_text"]))
    return "".join(pieces).strip()


def _contains_refusal(payload: Mapping[str, Any]) -> bool:
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    for message in output:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "refusal":
                continue
            refusal = item.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                return True
    return False


def _parse_usage(value: object) -> ProviderUsage:
    usage = value if isinstance(value, Mapping) else {}
    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, Mapping):
        input_details = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, Mapping):
        output_details = {}
    input_tokens = _token_count(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = _token_count(
        usage.get("output_tokens", usage.get("completion_tokens"))
    )
    cached_input_tokens = _token_count(
        input_details.get("cached_tokens", usage.get("cached_input_tokens"))
    )
    cache_write_input_tokens = _token_count(
        input_details.get(
            "cache_write_tokens",
            input_details.get(
                "cache_creation_tokens",
                usage.get("cache_write_input_tokens"),
            ),
        )
    )
    reasoning_tokens = _token_count(
        output_details.get("reasoning_tokens", usage.get("reasoning_tokens"))
    )
    total_tokens = _token_count(usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return ProviderUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _usage_is_complete(value: object, parsed: ProviderUsage) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw_counts = []
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
        raw_counts.append(raw)
    raw_input, raw_output, raw_total = raw_counts
    input_details = value.get("input_tokens_details")
    if not isinstance(input_details, Mapping):
        return False
    for key in ("cached_tokens", "cache_write_tokens"):
        raw = input_details.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return False
    output_details = value.get("output_tokens_details")
    if output_details is not None and not isinstance(output_details, Mapping):
        return False
    detail_values = []
    detail_values.extend(
        input_details.get(key)
        for key in (
            "cached_tokens",
            "cache_write_tokens",
            "cache_creation_tokens",
        )
        if key in input_details
    )
    detail_values.extend(
        value.get(key)
        for key in (
            "cached_input_tokens",
            "cache_write_input_tokens",
        )
        if key in value
    )
    if isinstance(output_details, Mapping) and "reasoning_tokens" in output_details:
        detail_values.append(output_details.get("reasoning_tokens"))
    if "reasoning_tokens" in value:
        detail_values.append(value.get("reasoning_tokens"))
    if any(
        isinstance(raw, bool) or not isinstance(raw, int) or raw < 0
        for raw in detail_values
    ):
        return False
    return bool(
        raw_input > 0
        and raw_output >= 0
        and raw_total == raw_input + raw_output
        and parsed.cached_input_tokens + parsed.cache_write_input_tokens <= raw_input
        and parsed.reasoning_tokens <= raw_output
        and parsed.input_tokens == raw_input
        and parsed.output_tokens == raw_output
        and parsed.total_tokens == raw_total
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
    "MAX_RESPONSE_BYTES",
    "OPENAI_RESPONSES_URL",
    "OpenAIResponsesProvider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderKnownError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderUnknownError",
    "ProviderUsage",
]
