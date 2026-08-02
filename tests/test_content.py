import hashlib
import io
import socket
import sys
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
import unittest
import urllib.error


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.content import (  # noqa: E402
    CONTENT_EXTRACTOR_VERSION,
    ContentFetchError,
    ContentFetcher,
    ContentSecurityError,
    extract_main_content,
    normalize_content_text,
    truncate_content,
    validate_public_url,
)


PUBLIC_IPV4 = "93.184.216.34"


def public_resolver(host, port, family, socktype):
    del host
    return [(family or socket.AF_INET, socktype, 6, "", (PUBLIC_IPV4, port))]


def resolver_for(mapping):
    def resolve(host, port, family, socktype):
        del family
        addresses = mapping[host]
        answers = []
        for address in addresses:
            address_family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if address_family == socket.AF_INET6 else (address, port)
            answers.append((address_family, socktype, 6, "", sockaddr))
        return answers

    return resolve


class FakeResponse:
    def __init__(
        self,
        body=b"",
        *,
        status=200,
        headers=None,
        url=None,
        ignore_read_size=False,
    ) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.ignore_read_size = ignore_read_size
        self.closed = False

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if self.ignore_read_size:
            return self._body.read()
        return self._body.read(size)

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if response.url is None:
            response.url = request.full_url
        return response


def html_response(body, *, url=None, status=200, headers=None):
    merged = {"Content-Type": "text/html; charset=utf-8"}
    if headers:
        merged.update(headers)
    return FakeResponse(
        body.encode("utf-8") if isinstance(body, str) else body,
        status=status,
        headers=merged,
        url=url,
    )


class URLPolicyTests(unittest.TestCase):
    def test_only_allowlisted_http_and_https_urls_are_accepted(self) -> None:
        self.assertEqual(
            "https://example.com/article?q=1",
            validate_public_url(
                "https://EXAMPLE.com./article?q=1#section",
                ["example.com"],
                resolver=public_resolver,
            ),
        )
        self.assertEqual(
            "http://example.com/",
            validate_public_url(
                "http://example.com", ["example.com"], resolver=public_resolver
            ),
        )

        rejected = (
            "file:///etc/passwd",
            "ftp://example.com/article",
            "https://user:secret@example.com/article",
            "https://example.com:444/article",
            "https://other.example/article",
            "https://example.com/\x00article",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ContentSecurityError):
                validate_public_url(
                    url, ["example.com"], resolver=public_resolver
                )

    def test_private_local_and_mixed_dns_answers_are_rejected(self) -> None:
        unsafe_addresses = (
            "127.0.0.1",
            "10.0.0.2",
            "169.254.169.254",
            "192.168.1.10",
            "::1",
            "fc00::1",
            "fe80::1",
        )
        for address in unsafe_addresses:
            with self.subTest(address=address), self.assertRaisesRegex(
                ContentSecurityError, "non-public"
            ):
                validate_public_url(
                    "https://example.com/article",
                    ["example.com"],
                    resolver=resolver_for({"example.com": [address]}),
                )

        with self.assertRaisesRegex(ContentSecurityError, "non-public"):
            validate_public_url(
                "https://example.com/article",
                ["example.com"],
                resolver=resolver_for(
                    {"example.com": [PUBLIC_IPV4, "127.0.0.1"]}
                ),
            )

    def test_literal_private_ip_is_rejected_even_when_allowlisted(self) -> None:
        for url, allowed in (
            ("http://127.0.0.1/article", "127.0.0.1"),
            ("http://[::1]/article", "[::1]"),
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ContentSecurityError, "non-public"
            ):
                validate_public_url(url, [allowed], resolver=public_resolver)

    def test_localhost_names_are_always_rejected(self) -> None:
        for host in ("localhost", "api.localhost"):
            with self.subTest(host=host), self.assertRaisesRegex(
                ContentSecurityError, "localhost"
            ):
                validate_public_url(
                    "https://%s/article" % host,
                    [host],
                    # A compromised resolver claiming a public address must
                    # not make the reserved localhost name acceptable.
                    resolver=public_resolver,
                )

    def test_resolution_failures_and_empty_answers_fail_closed(self) -> None:
        def failing_resolver(*args):
            del args
            raise socket.gaierror("not found")

        for resolver in (failing_resolver, lambda *args: []):
            with self.subTest(resolver=resolver), self.assertRaises(
                ContentSecurityError
            ):
                validate_public_url(
                    "https://example.com/article",
                    ["example.com"],
                    resolver=resolver,
                )


class RedirectAndRequestTests(unittest.TestCase):
    def test_each_redirect_hop_is_allowlisted_and_resolved(self) -> None:
        first = FakeResponse(
            status=302,
            headers={"Location": "https://cdn.example.com/final"},
        )
        second = html_response(
            "<html><article><p>Allowed redirect text.</p></article></html>"
        )
        opener = FakeOpener(first, second)
        resolved = []

        def resolver(host, port, family, socktype):
            resolved.append(host)
            return public_resolver(host, port, family, socktype)

        fetcher = ContentFetcher(
            ["example.com", "cdn.example.com"],
            resolver=resolver,
            opener=opener,
        )
        snapshot = fetcher.fetch("https://example.com/start")

        self.assertEqual("https://cdn.example.com/final", snapshot.final_url)
        self.assertEqual(2, len(opener.calls))
        self.assertIn("example.com", resolved)
        self.assertIn("cdn.example.com", resolved)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_http_error_redirect_from_no_redirect_opener_is_handled_manually(self) -> None:
        headers = Message()
        headers["Location"] = "/final"
        redirect = urllib.error.HTTPError(
            "https://example.com/start",
            302,
            "Found",
            headers,
            io.BytesIO(b""),
        )
        opener = FakeOpener(
            redirect,
            # Lower-case plain-dict keys also obey HTTP case-insensitivity.
            FakeResponse(
                body=b"<article>redirect result</article>",
                headers={"content-type": "text/html; charset=utf-8"},
            ),
        )
        snapshot = ContentFetcher(
            ["example.com"], resolver=public_resolver, opener=opener
        ).fetch("https://example.com/start")
        self.assertEqual("https://example.com/final", snapshot.final_url)
        self.assertEqual("redirect result", snapshot.text)

    def test_redirect_to_unallowlisted_or_private_host_is_blocked_before_open(self) -> None:
        for location, allowed, mapping in (
            (
                "https://evil.example/steal",
                ["example.com"],
                {"example.com": [PUBLIC_IPV4]},
            ),
            (
                "https://cdn.example.com/steal",
                ["example.com", "cdn.example.com"],
                {
                    "example.com": [PUBLIC_IPV4],
                    "cdn.example.com": ["127.0.0.1"],
                },
            ),
        ):
            with self.subTest(location=location):
                response = FakeResponse(status=302, headers={"Location": location})
                opener = FakeOpener(response)
                fetcher = ContentFetcher(
                    allowed,
                    resolver=resolver_for(mapping),
                    opener=opener,
                )
                with self.assertRaises(ContentSecurityError):
                    fetcher.fetch("https://example.com/start")
                self.assertEqual(1, len(opener.calls))
                self.assertTrue(response.closed)

    def test_redirect_loops_and_limits_are_bounded(self) -> None:
        loop_opener = FakeOpener(
            FakeResponse(status=302, headers={"Location": "/b"}),
            FakeResponse(status=302, headers={"Location": "/a"}),
        )
        loop_fetcher = ContentFetcher(
            ["example.com"], resolver=public_resolver, opener=loop_opener
        )
        with self.assertRaisesRegex(ContentSecurityError, "loop"):
            loop_fetcher.fetch("https://example.com/a")

        limited_opener = FakeOpener(
            FakeResponse(status=302, headers={"Location": "/b"})
        )
        limited_fetcher = ContentFetcher(
            ["example.com"],
            resolver=public_resolver,
            opener=limited_opener,
            max_redirects=0,
        )
        with self.assertRaisesRegex(ContentSecurityError, "redirect limit"):
            limited_fetcher.fetch("https://example.com/a")

    def test_opener_cannot_silently_follow_a_redirect(self) -> None:
        response = html_response(
            "<article>text</article>", url="https://cdn.example.com/final"
        )
        opener = FakeOpener(response)
        fetcher = ContentFetcher(
            ["example.com", "cdn.example.com"],
            resolver=public_resolver,
            opener=opener,
        )
        with self.assertRaisesRegex(ContentSecurityError, "unvalidated redirect"):
            fetcher.fetch("https://example.com/start")
        self.assertTrue(response.closed)

    def test_request_has_no_cookie_or_authorization_and_uses_timeout(self) -> None:
        opener = FakeOpener(
            html_response("<article><p>Safe request.</p></article>")
        )
        fetcher = ContentFetcher(
            ["example.com"],
            timeout_seconds=7.5,
            resolver=public_resolver,
            opener=opener,
        )
        fetcher.fetch("https://example.com/article")
        request, timeout = opener.calls[0]
        headers = {key.lower(): value for key, value in request.header_items()}

        self.assertEqual("GET", request.get_method())
        self.assertEqual(7.5, timeout)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertNotIn("proxy-authorization", headers)
        self.assertEqual("identity", headers["accept-encoding"])
        self.assertIn("text/html", headers["accept"])


class ResponseBoundaryTests(unittest.TestCase):
    def fetch_with_response(self, response, **options):
        fetcher = ContentFetcher(
            ["example.com"],
            resolver=public_resolver,
            opener=FakeOpener(response),
            **options,
        )
        return fetcher.fetch("https://example.com/article")

    def test_content_type_and_encoding_are_strict(self) -> None:
        responses = (
            FakeResponse(body=b"{}", headers={"Content-Type": "application/json"}),
            FakeResponse(body=b"plain", headers={}),
            FakeResponse(
                body=b"compressed",
                headers={
                    "Content-Type": "text/html",
                    "Content-Encoding": "gzip",
                },
            ),
        )
        for response in responses:
            with self.subTest(headers=response.headers), self.assertRaises(
                ContentFetchError
            ):
                self.fetch_with_response(response)
            self.assertTrue(response.closed)

    def test_announced_and_streamed_response_size_are_bounded(self) -> None:
        announced = FakeResponse(
            body=b"small",
            headers={"Content-Type": "text/html", "Content-Length": "1001"},
        )
        with self.assertRaisesRegex(ContentFetchError, "exceeds 1000 bytes"):
            self.fetch_with_response(announced, max_response_bytes=1000)

        streamed = FakeResponse(
            body=b"x" * 1001,
            headers={"Content-Type": "text/html"},
            ignore_read_size=True,
        )
        with self.assertRaisesRegex(ContentFetchError, "exceeds 1000 bytes"):
            self.fetch_with_response(streamed, max_response_bytes=1000)

        malformed = FakeResponse(
            body=b"x",
            headers={"Content-Type": "text/html", "Content-Length": "nope"},
        )
        with self.assertRaisesRegex(ContentFetchError, "invalid Content-Length"):
            self.fetch_with_response(malformed)

    def test_non_success_and_network_errors_are_wrapped(self) -> None:
        response = FakeResponse(
            body=b"error", status=503, headers={"Content-Type": "text/html"}
        )
        with self.assertRaises(ContentFetchError) as raised:
            self.fetch_with_response(response)
        self.assertEqual(503, raised.exception.status)

        network_error = urllib.error.URLError("offline")
        fetcher = ContentFetcher(
            ["example.com"],
            resolver=public_resolver,
            opener=FakeOpener(network_error),
        )
        with self.assertRaisesRegex(ContentFetchError, "network error"):
            fetcher.fetch("https://example.com/article")

    def test_charset_header_and_meta_charset_are_honored(self) -> None:
        latin = "<html><article><p>café naïve</p></article></html>".encode(
            "iso-8859-1"
        )
        header_snapshot = self.fetch_with_response(
            FakeResponse(
                body=latin,
                headers={"Content-Type": "text/html; charset=iso-8859-1"},
            )
        )
        self.assertEqual("café naïve", header_snapshot.text)

        meta_body = (
            b'<html><head><meta charset="windows-1252"></head>'
            b"<article><p>price \x8010</p></article></html>"
        )
        meta_snapshot = self.fetch_with_response(
            FakeResponse(body=meta_body, headers={"Content-Type": "text/html"})
        )
        self.assertEqual("price €10", meta_snapshot.text)


class ExtractionAndSnapshotTests(unittest.TestCase):
    def test_article_is_preferred_and_chrome_is_removed(self) -> None:
        document = """
            <!doctype html>
            <html>
              <head><title> Example   Article </title><style>bad style</style></head>
              <body>
                <nav>Navigation noise</nav>
                <main>
                  <p>Main wrapper noise.</p>
                  <article>
                    <header><h1>Deterministic heading</h1></header>
                    <p>First&nbsp;paragraph with <strong>important</strong> text.</p>
                    <aside>Sidebar noise</aside>
                    <script>malicious instruction</script>
                    <p>Second paragraph.</p>
                    <div class="share">Share this article</div>
                  </article>
                </main>
                <footer>Footer noise</footer>
              </body>
            </html>
        """
        title, text = extract_main_content(document)

        self.assertEqual("Example Article", title)
        self.assertEqual(
            "Deterministic heading\n\n"
            "First paragraph with important text.\n\n"
            "Second paragraph.",
            text,
        )
        for noise in (
            "Navigation noise",
            "Main wrapper noise",
            "Sidebar noise",
            "malicious instruction",
            "Share this article",
            "Footer noise",
        ):
            self.assertNotIn(noise, text)

    def test_main_then_named_content_then_body_are_deterministic_fallbacks(self) -> None:
        cases = (
            (
                "<body>body<main><p>main content</p></main></body>",
                "main content",
            ),
            (
                '<body>body<div class="post-content"><p>named content</p></div></body>',
                "named content",
            ),
            ("<body><p>body content</p><nav>noise</nav></body>", "body content"),
        )
        for document, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, extract_main_content(document)[1])

        # An empty article shell must not hide usable main content.
        self.assertEqual(
            "main fallback",
            extract_main_content(
                "<body><main><p>main fallback</p><article></article></main></body>"
            )[1],
        )

    def test_normalization_and_truncation_are_stable(self) -> None:
        self.assertEqual(
            "One two\n\nThree four",
            normalize_content_text(" One\t two\r\n\r\n\r\n Three\u00a0four\x00 "),
        )
        self.assertEqual(("short", False), truncate_content("short", 5))
        retained, truncated = truncate_content("one two three four five", 14)
        self.assertTrue(truncated)
        self.assertLessEqual(len(retained), 14)
        self.assertTrue(retained.endswith("…"))
        with self.assertRaises(ValueError):
            truncate_content("text", 0)

    def test_snapshot_records_full_and_retained_hashes_and_truncation(self) -> None:
        document = (
            "<html><head><title>Hash Test</title></head><article>"
            "<p>First paragraph has enough content for a stable boundary.</p>"
            "<p>Second paragraph makes the complete extracted text longer.</p>"
            "</article></html>"
        )
        full_text = extract_main_content(document)[1]
        response = html_response(
            document,
            headers={
                "ETag": '"article-v1"',
                "Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT",
            },
        )
        fetcher = ContentFetcher(
            ["example.com"],
            resolver=public_resolver,
            opener=FakeOpener(response),
            max_characters=70,
            clock=lambda: datetime(2026, 8, 1, 12, 34, 56, tzinfo=timezone.utc),
        )
        snapshot = fetcher.fetch("https://example.com/article#ignored")

        self.assertEqual("https://example.com/article", snapshot.source_url)
        self.assertEqual("https://example.com/article", snapshot.final_url)
        self.assertEqual("2026-08-01T12:34:56Z", snapshot.fetched_at)
        self.assertEqual("Hash Test", snapshot.title)
        self.assertEqual('"article-v1"', snapshot.etag)
        self.assertEqual(CONTENT_EXTRACTOR_VERSION, snapshot.extractor_version)
        self.assertTrue(snapshot.truncated)
        self.assertEqual(len(full_text), snapshot.original_character_count)
        self.assertEqual(len(snapshot.text), snapshot.character_count)
        self.assertEqual(len(snapshot.text.encode("utf-8")), snapshot.utf8_bytes)
        self.assertEqual(
            hashlib.sha256(document.encode("utf-8")).hexdigest(),
            snapshot.source_body_sha256,
        )
        self.assertEqual(
            hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
            snapshot.full_text_sha256,
        )
        self.assertEqual(
            hashlib.sha256(snapshot.text.encode("utf-8")).hexdigest(),
            snapshot.text_sha256,
        )
        self.assertNotEqual(snapshot.full_text_sha256, snapshot.text_sha256)

    def test_empty_document_fails_without_a_snapshot(self) -> None:
        fetcher = ContentFetcher(
            ["example.com"],
            resolver=public_resolver,
            opener=FakeOpener(html_response("<html><script>x</script></html>")),
        )
        with self.assertRaisesRegex(ContentFetchError, "no extractable"):
            fetcher.fetch("https://example.com/empty")

    def test_constructor_limits_fail_closed(self) -> None:
        invalid_options = (
            {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")},
            {"max_response_bytes": 0},
            {"max_characters": 0},
            {"max_redirects": -1},
            {"max_redirects": 21},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                ContentFetcher(["example.com"], **options)


if __name__ == "__main__":
    unittest.main()
