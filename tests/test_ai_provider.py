import io
import json
import os
import sys
from email.message import Message
from pathlib import Path
import unittest
from unittest import mock
import urllib.error


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.ai_provider import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    OPENAI_RESPONSES_URL,
    OpenAIResponsesProvider,
    ProviderConfigError,
    ProviderHTTPError,
    ProviderKnownError,
    ProviderRequest,
    ProviderUnknownError,
    ProviderUsage,
    _NoRedirectHandler,
)


API_KEY = "test-secret-api-key"


def provider_request(**overrides) -> ProviderRequest:
    values = {
        "model": "example-reasoning-model",
        "instructions": "Return only the requested structured result.",
        "input_text": '{"article_id":17,"title":"An article"}',
        "json_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "idempotency_key": "article-17.summary.zh-CN.v1",
        "max_output_tokens": 384,
        "schema_name": "article_summary",
    }
    values.update(overrides)
    return ProviderRequest(**values)


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None, raw_body=None) -> None:
        if raw_body is not None:
            body = raw_body
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode("utf-8")
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or Message()
        self.reason = "status reason"
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_calls += 1
        return self.body.read(size)


class RecordingOpener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class OpenAIResponsesProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": API_KEY}
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def test_posts_strict_schema_without_tools_and_extracts_later_message(self) -> None:
        headers = Message()
        headers["x-request-id"] = "http-request-123"
        response = FakeResponse(
            {
                "id": "resp_123",
                "status": "completed",
                "model": "resolved-model-2026-08-01",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [
                            {"type": "refusal", "refusal": ""},
                            {
                                "type": "output_text",
                                "text": '{"summary":"中文摘要"}',
                            },
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 101,
                    "input_tokens_details": {
                        "cached_tokens": 40,
                        "cache_write_tokens": 11,
                    },
                    "output_tokens": 23,
                    "output_tokens_details": {"reasoning_tokens": 7},
                    "total_tokens": 124,
                },
            },
            headers=headers,
        )
        opener = RecordingOpener(response)
        provider = OpenAIResponsesProvider(
            opener=opener,
            timeout_seconds=12,
        )

        result = provider.generate(provider_request())

        self.assertEqual('{"summary":"中文摘要"}', result.output_text)
        self.assertEqual("resolved-model-2026-08-01", result.model)
        self.assertEqual("http-request-123", result.request_id)
        self.assertEqual("resp_123", result.response_id)
        self.assertTrue(result.usage_reported)
        self.assertEqual(
            ProviderUsage(
                input_tokens=101,
                cached_input_tokens=40,
                cache_write_input_tokens=11,
                output_tokens=23,
                reasoning_tokens=7,
                total_tokens=124,
            ),
            result.usage,
        )

        self.assertEqual(1, len(opener.calls))
        request, timeout = opener.calls[0]
        self.assertEqual(12.0, timeout)
        self.assertEqual(OPENAI_RESPONSES_URL, request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("Bearer %s" % API_KEY, request.get_header("Authorization"))
        self.assertEqual(
            "article-17.summary.zh-CN.v1",
            request.get_header("Idempotency-key"),
        )
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("example-reasoning-model", payload["model"])
        self.assertEqual("medium", payload["reasoning"]["effort"])
        self.assertEqual(384, payload["max_output_tokens"])
        self.assertIs(False, payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(
            {
                "type": "json_schema",
                "name": "article_summary",
                "strict": True,
                "schema": provider_request().json_schema,
            },
            payload["text"]["format"],
        )

    def test_missing_key_and_invalid_request_fail_before_transport(self) -> None:
        opener = RecordingOpener(AssertionError("transport must not be called"))
        without_key = OpenAIResponsesProvider(opener=opener)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderConfigError, "OPENAI_API_KEY"):
                without_key.generate(provider_request())

        provider = OpenAIResponsesProvider(opener=opener)
        for request in (
            provider_request(idempotency_key="bad\r\nheader"),
            provider_request(max_output_tokens=0),
            provider_request(schema_name="bad schema name"),
        ):
            with self.subTest(request=request):
                with self.assertRaises(ProviderConfigError):
                    provider.generate(request)
        self.assertEqual([], opener.calls)

    def test_json_schema_serialization_error_is_local_configuration_error(self) -> None:
        opener = RecordingOpener(AssertionError("transport must not be called"))
        provider = OpenAIResponsesProvider(opener=opener)
        with self.assertRaisesRegex(ProviderConfigError, "not JSON serializable"):
            provider.generate(provider_request(json_schema={"invalid": {1, 2}}))
        self.assertEqual([], opener.calls)

    def test_http_429_is_retryable_and_does_not_read_or_disclose_body(self) -> None:
        headers = Message()
        headers["Retry-After"] = "17"
        secret_body = io.BytesIO(
            ("provider detail containing %s and private input" % API_KEY).encode()
        )
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL,
            429,
            "rate limited",
            headers,
            secret_body,
        )
        opener = RecordingOpener(error)
        provider = OpenAIResponsesProvider(opener=opener)

        with self.assertRaises(ProviderHTTPError) as raised:
            provider.generate(provider_request())
        self.assertEqual(429, raised.exception.status)
        self.assertEqual(17.0, raised.exception.retry_after)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn(API_KEY, str(raised.exception))
        self.assertNotIn("private input", str(raised.exception))
        self.assertEqual(1, len(opener.calls))
        self.assertTrue(secret_body.closed)

    def test_nonretryable_http_error_and_redirect_are_not_followed(self) -> None:
        for status in (302, 400, 401, 403, 422):
            with self.subTest(status=status):
                response = FakeResponse({}, status=status)
                opener = RecordingOpener(response)
                provider = OpenAIResponsesProvider(opener=opener)
                with self.assertRaises(ProviderHTTPError) as raised:
                    provider.generate(provider_request())
                self.assertEqual(status, raised.exception.status)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(0, response.read_calls)
                self.assertEqual(1, len(opener.calls))

        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                mock.Mock(),
                mock.Mock(),
                302,
                "Found",
                Message(),
                "https://attacker.example/steal",
            )
        )

    def test_server_error_is_retryable_but_never_retried_internally(self) -> None:
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL, 503, "temporarily unavailable", Message(), None
        )
        opener = RecordingOpener(error)
        provider = OpenAIResponsesProvider(opener=opener)
        with self.assertRaises(ProviderHTTPError) as raised:
            provider.generate(provider_request())
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(1, len(opener.calls))

    def test_transport_failure_is_unknown_redacted_truncated_and_not_retried(self) -> None:
        detail = "%s %s" % (API_KEY, "x" * 1000)
        opener = RecordingOpener(urllib.error.URLError(detail))
        provider = OpenAIResponsesProvider(opener=opener)
        with self.assertRaises(ProviderUnknownError) as raised:
            provider.generate(provider_request())
        message = str(raised.exception)
        self.assertNotIn(API_KEY, message)
        self.assertIn("[redacted]", message)
        self.assertIn("may already have been processed", message)
        self.assertLess(len(message), 400)
        self.assertEqual(1, len(opener.calls))

    def test_response_size_limit_content_length_and_streaming(self) -> None:
        large_headers = Message()
        large_headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
        declared_large = FakeResponse({}, headers=large_headers)
        streamed_large = FakeResponse(None, raw_body=b"x" * (MAX_RESPONSE_BYTES + 1))
        for response in (declared_large, streamed_large):
            with self.subTest(declared=bool(response.headers.get("Content-Length"))):
                opener = RecordingOpener(response)
                provider = OpenAIResponsesProvider(opener=opener)
                with self.assertRaisesRegex(ProviderUnknownError, "configured byte limit"):
                    provider.generate(provider_request())
                self.assertEqual(1, len(opener.calls))
        self.assertEqual(0, declared_large.read_calls)
        self.assertEqual(1, streamed_large.read_calls)

        lower_limit = FakeResponse(None, raw_body=b"x" * 1_025)
        provider = OpenAIResponsesProvider(
            opener=RecordingOpener(lower_limit), max_response_bytes=1_024
        )
        with self.assertRaisesRegex(ProviderUnknownError, "configured byte limit"):
            provider.generate(provider_request())

    def test_invalid_json_missing_output_and_nonobject_are_unknown(self) -> None:
        responses = (
            FakeResponse(None, raw_body=b"not json"),
            FakeResponse({"id": "resp_without_output", "output": []}),
            FakeResponse(["not", "an", "object"]),
        )
        for response in responses:
            with self.subTest(body=response.body.getvalue()[:30]):
                provider = OpenAIResponsesProvider(
                    opener=RecordingOpener(response),
                )
                with self.assertRaises(ProviderUnknownError):
                    provider.generate(provider_request())

    def test_usage_is_defensive_and_ids_fall_back_to_response_and_request(self) -> None:
        response = FakeResponse(
            {
                "id": "resp_fallback",
                "status": "completed",
                "output": [
                    {"type": "output_text", "text": '{"summary":"ok"}'}
                ],
                "usage": {
                    "prompt_tokens": "9",
                    "completion_tokens": 4,
                    "cached_input_tokens": -10,
                    "reasoning_tokens": True,
                    "cache_write_input_tokens": "3",
                    "total_tokens": None,
                },
            }
        )
        provider = OpenAIResponsesProvider(
            opener=RecordingOpener(response),
        )
        result = provider.generate(provider_request())
        self.assertFalse(result.usage_reported)
        self.assertEqual("example-reasoning-model", result.model)
        self.assertEqual("resp_fallback", result.request_id)
        self.assertEqual("resp_fallback", result.response_id)
        self.assertEqual(
            ProviderUsage(
                input_tokens=9,
                cached_input_tokens=0,
                cache_write_input_tokens=3,
                output_tokens=4,
                reasoning_tokens=0,
                total_tokens=13,
            ),
            result.usage,
        )

    def test_noncompleted_error_and_refusal_are_known_when_usage_is_exact(self) -> None:
        cases = (
            (
                "response_status",
                {
                    "id": "resp_missing_status",
                    "output_text": '{"summary":"unsafe"}',
                },
            ),
            (
                "incomplete",
                {
                    "id": "resp_incomplete",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output_text": '{"summary":"partial"}',
                },
            ),
            (
                "response_error",
                {
                    "id": "resp_failed",
                    "status": "completed",
                    "error": {"code": "internal_error", "message": "secret detail"},
                    "output_text": '{"summary":"unsafe"}',
                },
            ),
            (
                "refusal",
                {
                    "id": "resp_refusal",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "refusal", "refusal": "private refusal text"}
                            ],
                        }
                    ],
                },
            ),
        )
        for code, payload in cases:
            payload["usage"] = {
                "input_tokens": 10,
                "input_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                },
                "output_tokens": 2,
                "total_tokens": 12,
            }
            with self.subTest(code=code):
                provider = OpenAIResponsesProvider(
                    opener=RecordingOpener(FakeResponse(payload))
                )
                with self.assertRaises(ProviderKnownError) as raised:
                    provider.generate(provider_request())
                self.assertEqual(code, raised.exception.code)
                self.assertEqual(12, raised.exception.usage.total_tokens)
                self.assertNotIn("secret detail", str(raised.exception))
                self.assertNotIn("private refusal text", str(raised.exception))

        provider = OpenAIResponsesProvider(
            opener=RecordingOpener(
                FakeResponse(
                    {
                        "id": "resp_incomplete_unknown_usage",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                    }
                )
            )
        )
        with self.assertRaises(ProviderUnknownError):
            provider.generate(provider_request())

    def test_completed_response_without_usage_is_marked_unreported(self) -> None:
        for usage in (
            None,
            {"total_tokens": 1},
            {"input_tokens": 1, "output_tokens": 1},
            {
                "input_tokens": 100,
                "output_tokens": 2,
                "total_tokens": 102,
                "input_tokens_details": {"cached_tokens": 0},
            },
            {
                "input_tokens": 100,
                "output_tokens": 2,
                "total_tokens": 102,
                "input_tokens_details": {"cache_write_tokens": 0},
            },
            {
                "input_tokens": 100,
                "output_tokens": 2,
                "total_tokens": 102,
                "input_tokens_details": {
                    "cached_tokens": "1000",
                    "cache_write_tokens": 0,
                },
            },
            {
                "input_tokens": 100,
                "output_tokens": 2,
                "total_tokens": 102,
                "input_tokens_details": {
                    "cached_tokens": 60,
                    "cache_write_tokens": 60,
                },
            },
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 3,
                "input_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                },
            },
        ):
            payload = {
                "id": "resp_no_usage",
                "status": "completed",
                "output_text": '{"summary":"ok"}',
            }
            if usage is not None:
                payload["usage"] = usage
            with self.subTest(usage=usage):
                provider = OpenAIResponsesProvider(
                    opener=RecordingOpener(FakeResponse(payload))
                )
                result = provider.generate(provider_request())
                self.assertFalse(result.usage_reported)


if __name__ == "__main__":
    unittest.main()
