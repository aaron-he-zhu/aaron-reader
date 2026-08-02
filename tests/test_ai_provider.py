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
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DEEPSEEK_MODEL,
    MAX_RESPONSE_BYTES,
    DeepSeekChatCompletionsProvider,
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
        "model": DEEPSEEK_MODEL,
        "instructions": "Return only the requested structured JSON result.",
        "input_text": '{"article_id":17,"title":"An article"}',
        "json_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        "idempotency_key": "article-17.summary.zh-CN.v1",
        "max_output_tokens": 384,
        "reasoning_effort": "none",
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


class DeepSeekChatCompletionsProviderTests(unittest.TestCase):
    @staticmethod
    def successful_payload(content='{"summary":"ok"}'):
        return {
            "id": "chatcmpl-deepseek-1",
            "object": "chat.completion",
            "model": DEEPSEEK_MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 60,
                "completion_tokens": 20,
                "total_tokens": 120,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }

    def test_fixed_json_mode_request_has_no_tools_store_or_reasoning_effort(self) -> None:
        headers = Message()
        headers["x-request-id"] = "deepseek-request-1"
        opener = RecordingOpener(
            FakeResponse(self.successful_payload(), headers=headers)
        )
        provider = DeepSeekChatCompletionsProvider(
            opener=opener,
            timeout_seconds=9,
        )
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": API_KEY},
            clear=True,
        ):
            result = provider.generate(provider_request())

        self.assertEqual('{"summary":"ok"}', result.output_text)
        self.assertEqual("deepseek-request-1", result.request_id)
        self.assertEqual("chatcmpl-deepseek-1", result.response_id)
        self.assertEqual(DEEPSEEK_MODEL, result.model)
        self.assertTrue(result.usage_reported)
        self.assertEqual(
            ProviderUsage(
                input_tokens=100,
                cached_input_tokens=40,
                output_tokens=20,
                total_tokens=120,
            ),
            result.usage,
        )

        self.assertEqual(1, len(opener.calls))
        request, timeout = opener.calls[0]
        self.assertEqual(9.0, timeout)
        self.assertEqual(DEEPSEEK_CHAT_COMPLETIONS_URL, request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("Bearer %s" % API_KEY, request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Idempotency-Key"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(DEEPSEEK_MODEL, payload["model"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertIs(False, payload["stream"])
        self.assertEqual(384, payload["max_tokens"])
        self.assertEqual(
            ["system", "user"],
            [item["role"] for item in payload["messages"]],
        )
        self.assertIn("Return JSON only", payload["messages"][0]["content"])
        self.assertIn("JSON Schema:", payload["messages"][0]["content"])
        for forbidden in (
            "tools",
            "tool_choice",
            "store",
            "reasoning_effort",
        ):
            self.assertNotIn(forbidden, payload)

    def test_only_fixed_key_and_model_are_accepted_before_transport(self) -> None:
        opener = RecordingOpener(AssertionError("transport must not be called"))
        provider = DeepSeekChatCompletionsProvider(opener=opener)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderConfigError, "DEEPSEEK_API_KEY"):
                provider.generate(provider_request())
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True):
            with self.assertRaisesRegex(ProviderConfigError, DEEPSEEK_MODEL):
                provider.generate(provider_request(model="deepseek-v4-pro"))
        self.assertEqual([], opener.calls)

    def test_invalid_local_options_fail_before_transport(self) -> None:
        for options in (
            {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")},
            {"max_response_bytes": 100},
            {"max_response_bytes": MAX_RESPONSE_BYTES + 1},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ProviderConfigError):
                    DeepSeekChatCompletionsProvider(**options)

        opener = RecordingOpener(AssertionError("transport must not be called"))
        provider = DeepSeekChatCompletionsProvider(opener=opener)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True):
            for request in (
                provider_request(idempotency_key="bad\r\nheader"),
                provider_request(max_output_tokens=0),
                provider_request(schema_name="bad schema"),
                provider_request(json_schema={"bad": {1, 2}}),
            ):
                with self.subTest(request=request):
                    with self.assertRaises(ProviderConfigError):
                        provider.generate(request)
        self.assertEqual([], opener.calls)

    def test_non_stop_tool_or_thinking_outputs_are_known_and_usage_is_auditable(self) -> None:
        cases = (
            ("finish_length", {"finish_reason": "length"}),
            (
                "tool_calls",
                {
                    "message": {
                        "role": "assistant",
                        "content": "{}",
                        "tool_calls": [{"id": "x"}],
                    }
                },
            ),
            (
                "thinking_output",
                {
                    "message": {
                        "role": "assistant",
                        "content": "{}",
                        "reasoning_content": "private chain",
                    }
                },
            ),
        )
        for expected_code, choice_overrides in cases:
            payload = self.successful_payload()
            payload["choices"][0].update(choice_overrides)
            provider = DeepSeekChatCompletionsProvider(
                opener=RecordingOpener(FakeResponse(payload))
            )
            with self.subTest(code=expected_code), mock.patch.dict(
                os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True
            ):
                with self.assertRaises(ProviderKnownError) as raised:
                    provider.generate(provider_request())
                self.assertEqual(expected_code, raised.exception.code)
                self.assertEqual(120, raised.exception.usage.total_tokens)
                self.assertNotIn("private chain", str(raised.exception))

    def test_incomplete_usage_is_not_claimed_as_audited(self) -> None:
        payload = self.successful_payload()
        payload["usage"].pop("prompt_cache_miss_tokens")
        provider = DeepSeekChatCompletionsProvider(
            opener=RecordingOpener(FakeResponse(payload))
        )
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True
        ):
            result = provider.generate(provider_request())
        self.assertFalse(result.usage_reported)

    def test_http_error_body_and_transport_secret_are_never_disclosed(self) -> None:
        secret_body = io.BytesIO((API_KEY + " private body").encode("utf-8"))
        http_error = urllib.error.HTTPError(
            DEEPSEEK_CHAT_COMPLETIONS_URL,
            429,
            "rate limited",
            Message(),
            secret_body,
        )
        for outcome in (
            http_error,
            urllib.error.URLError(API_KEY + " transport"),
        ):
            provider = DeepSeekChatCompletionsProvider(
                opener=RecordingOpener(outcome)
            )
            with self.subTest(outcome=type(outcome).__name__), mock.patch.dict(
                os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True
            ):
                with self.assertRaises(
                    (ProviderHTTPError, ProviderUnknownError)
                ) as raised:
                    provider.generate(provider_request())
                self.assertNotIn(API_KEY, str(raised.exception))
                self.assertNotIn("private body", str(raised.exception))

    def test_response_size_and_invalid_json_are_bounded_unknown_results(self) -> None:
        large_headers = Message()
        large_headers["Content-Length"] = str(MAX_RESPONSE_BYTES + 1)
        responses = (
            FakeResponse({}, headers=large_headers),
            FakeResponse(None, raw_body=b"x" * (MAX_RESPONSE_BYTES + 1)),
            FakeResponse(None, raw_body=b"not-json"),
            FakeResponse(["not", "an", "object"]),
        )
        for response in responses:
            provider = DeepSeekChatCompletionsProvider(
                opener=RecordingOpener(response)
            )
            with self.subTest(size=len(response.body.getvalue())), mock.patch.dict(
                os.environ, {"DEEPSEEK_API_KEY": API_KEY}, clear=True
            ):
                with self.assertRaises(ProviderUnknownError):
                    provider.generate(provider_request())

    def test_redirect_handler_never_forwards_authorization(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
