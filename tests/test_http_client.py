import gzip
import io
import math
import sys
from email.message import Message
from pathlib import Path
import unittest
from unittest import mock
import urllib.error


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.http_client import FetchError, HttpClient  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes, headers=None) -> None:
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size=-1):
        return self.body.read(size)

    def getcode(self):
        return 200

    def geturl(self):
        return "https://example.com/feed"


class HttpClientTests(unittest.TestCase):
    def test_negative_and_nonfinite_retry_after_are_safe(self) -> None:
        self.assertEqual(0.0, HttpClient._retry_delay(0, "-1"))
        fallback = HttpClient._retry_delay(0, "nan")
        self.assertTrue(math.isfinite(fallback))
        self.assertGreaterEqual(fallback, 1.0)
        with mock.patch("aaron_reader.http_client.time.sleep") as sleep:
            HttpClient._wait_before_retry(0, "-1")
        sleep.assert_called_once_with(0.0)

    def test_long_retry_after_is_not_slept_or_retried_in_process(self) -> None:
        headers = Message()
        headers["Retry-After"] = "3600"
        error = urllib.error.HTTPError(
            "https://example.com/feed", 429, "rate limited", headers, None
        )
        client = HttpClient(min_host_interval_seconds=0)
        with mock.patch("aaron_reader.http_client.urllib.request.urlopen", side_effect=error) as open_url:
            with mock.patch("aaron_reader.http_client.time.sleep") as sleep:
                with self.assertRaises(FetchError) as raised:
                    client.fetch("https://example.com/feed", attempts=2)
        self.assertEqual(1, open_url.call_count)
        sleep.assert_not_called()
        self.assertEqual(429, raised.exception.status)
        self.assertEqual(3600.0, raised.exception.retry_after_seconds)

    def test_gzip_decompression_is_bounded(self) -> None:
        compressed = gzip.compress(b"x" * 50_000)
        response = FakeResponse(compressed, {"Content-Encoding": "gzip"})
        client = HttpClient(max_response_bytes=1_000, min_host_interval_seconds=0)
        with mock.patch(
            "aaron_reader.http_client.urllib.request.urlopen", return_value=response
        ):
            with self.assertRaisesRegex(FetchError, "exceeds 1000 bytes"):
                client.fetch("https://example.com/feed", attempts=1)


if __name__ == "__main__":
    unittest.main()
