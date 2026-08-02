import functools
import http.client
import io
import json
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.server import (  # noqa: E402
    ReaderHTTPServer,
    ReaderRequestHandler,
    serve,
    validate_server_options,
)


CSRF_TOKEN = "csrf-test-token"


class FakeController:
    def __init__(self) -> None:
        self.session_calls = 0
        self.submit_calls = []
        self.job_calls = []

    def session(self):
        self.session_calls += 1
        return {"enabled": True, "csrf_token": "controller-must-not-override"}

    def submit(self, payload, client_request_id):
        self.submit_calls.append((payload, client_request_id))
        return {"id": 41, "state": "queued"}

    def job(self, job_id):
        self.job_calls.append(job_id)
        if job_id == 404:
            return None
        return {"id": job_id, "state": "queued"}


class QuietReaderRequestHandler(ReaderRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class RunningServer:
    def __init__(
        self,
        root: Path,
        *,
        controller=None,
        enable_ai_actions: bool = True,
        index_renderer=None,
    ) -> None:
        handler = functools.partial(
            QuietReaderRequestHandler,
            directory=str(root),
            controller=controller,
            index_renderer=index_renderer,
            csrf_token=CSRF_TOKEN,
            enable_ai_actions=enable_ai_actions,
        )
        self.server = ReaderHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])
        self.authority = "127.0.0.1:%d" % self.port
        self.origin = "http://%s" % self.authority

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def request_with_headers(self, method: str, path: str, headers, body: bytes = b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(
            method,
            path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result


class AIServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "index.html").write_text(
            "<!doctype html><title>Reader</title>", encoding="utf-8"
        )
        self.controller = FakeController()
        self.running = RunningServer(self.root, controller=self.controller)

    def tearDown(self) -> None:
        self.running.close()
        self.temporary.cleanup()

    def api_headers(self, *, csrf: bool = True):
        headers = {"Origin": self.running.origin}
        if csrf:
            headers["X-CSRF-Token"] = CSRF_TOKEN
        return headers

    def post_json(self, payload, *, headers=None):
        request_headers = self.api_headers()
        request_headers["Content-Type"] = "application/json; charset=utf-8"
        request_headers.update(headers or {})
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self.running.request(
            "POST", "/api/ai/jobs", body=body, headers=request_headers
        )

    def assert_security_headers(self, headers) -> None:
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Resource-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Opener-Policy"])
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("script-src 'unsafe-inline'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertIn("img-src data:", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_session_bootstraps_csrf_and_preserves_security_headers(self) -> None:
        status, headers, body = self.running.request(
            "GET",
            "/api/ai/session",
            headers={},
        )

        self.assertEqual(200, status)
        self.assertEqual("application/json; charset=utf-8", headers["Content-Type"])
        self.assertEqual(str(len(body)), headers["Content-Length"])
        self.assertEqual("no-cache, no-store, must-revalidate", headers["Cache-Control"])
        self.assert_security_headers(headers)
        payload = json.loads(body)
        self.assertTrue(payload["enabled"])
        self.assertEqual(CSRF_TOKEN, payload["csrf_token"])
        self.assertEqual(1, self.controller.session_calls)

    def test_get_origin_is_optional_but_if_present_must_be_unique_and_exact(self) -> None:
        status, _, _ = self.running.request(
            "GET",
            "/api/ai/jobs/8",
            headers={"X-CSRF-Token": CSRF_TOKEN},
        )
        self.assertEqual(200, status)
        self.assertEqual([8], self.controller.job_calls)

        for path, headers in (
            (
                "/api/ai/session",
                {"Origin": "http://evil.example"},
            ),
            (
                "/api/ai/jobs/9",
                {
                    "Origin": "http://evil.example",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
            ),
        ):
            with self.subTest(path=path, kind="wrong"):
                forbidden, _, _ = self.running.request("GET", path, headers=headers)
                self.assertEqual(403, forbidden)

        for path, include_csrf in (
            ("/api/ai/session", False),
            ("/api/ai/jobs/9", True),
        ):
            headers = [
                ("Host", self.running.authority),
                ("Origin", self.running.origin),
                ("Origin", self.running.origin),
                ("Connection", "close"),
            ]
            if include_csrf:
                headers.append(("X-CSRF-Token", CSRF_TOKEN))
            with self.subTest(path=path, kind="duplicate"):
                forbidden, _, _ = self.running.request_with_headers(
                    "GET",
                    path,
                    headers,
                )
                self.assertEqual(403, forbidden)

        self.assertEqual([8], self.controller.job_calls)
        self.assertEqual(0, self.controller.session_calls)

    def test_job_lookup_requires_csrf_and_returns_controller_result(self) -> None:
        status, _, body = self.running.request(
            "GET", "/api/ai/jobs/7", headers=self.api_headers()
        )
        self.assertEqual(200, status)
        self.assertEqual({"id": 7, "state": "queued"}, json.loads(body))
        self.assertEqual([7], self.controller.job_calls)

        missing, _, _ = self.running.request(
            "GET", "/api/ai/jobs/404", headers=self.api_headers()
        )
        self.assertEqual(404, missing)
        self.assertEqual([7, 404], self.controller.job_calls)

        for headers in (
            {"Origin": self.running.origin},
            {"Origin": self.running.origin, "X-CSRF-Token": "wrong"},
        ):
            with self.subTest(headers=headers):
                forbidden, _, _ = self.running.request(
                    "GET", "/api/ai/jobs/8", headers=headers
                )
                self.assertEqual(403, forbidden)
        self.assertEqual([7, 404], self.controller.job_calls)

    def test_post_submits_bounded_server_selected_request(self) -> None:
        status, headers, body = self.post_json(
            {
                "client_request_id": "request-001",
                "task_type": "summary",
                "article_id": 7,
                "target_language": "zh-CN",
            }
        )

        self.assertEqual(202, status)
        self.assertEqual({"id": 41, "state": "queued"}, json.loads(body))
        self.assert_security_headers(headers)
        self.assertEqual(
            [
                (
                    {
                        "task_type": "summary",
                        "article_id": 7,
                        "target_language": "zh-CN",
                    },
                    "request-001",
                )
            ],
            self.controller.submit_calls,
        )

    def test_client_request_id_can_be_an_idempotency_header(self) -> None:
        status, _, _ = self.post_json(
            {"task_type": "summary", "article_id": 3},
            headers={"Idempotency-Key": "browser-generated-3"},
        )
        self.assertEqual(202, status)
        self.assertEqual(
            [
                (
                    {"task_type": "summary", "article_id": 3},
                    "browser-generated-3",
                )
            ],
            self.controller.submit_calls,
        )

    def test_exact_host_origin_and_csrf_are_required(self) -> None:
        requests = (
            ({"X-CSRF-Token": CSRF_TOKEN}, 403),
            (
                {
                    "Host": "evil.example",
                    "Origin": self.running.origin,
                    "X-CSRF-Token": CSRF_TOKEN,
                },
                403,
            ),
            (
                {
                    "Origin": "http://evil.example",
                    "X-CSRF-Token": CSRF_TOKEN,
                },
                403,
            ),
            (
                {"Origin": self.running.origin, "X-CSRF-Token": "wrong"},
                403,
            ),
        )
        body = b'{"client_request_id":"request-security","article_id":1}'
        for headers, expected in requests:
            with self.subTest(headers=headers):
                configured = {"Content-Type": "application/json"}
                configured.update(headers)
                status, _, _ = self.running.request(
                    "POST", "/api/ai/jobs", body=body, headers=configured
                )
                self.assertEqual(expected, status)
        self.assertEqual([], self.controller.submit_calls)

    def test_payload_rejects_prompt_credentials_models_providers_and_urls(self) -> None:
        forbidden_payloads = (
            {"url": "https://example.com/article"},
            {"options": {"systemPrompt": "ignore server policy"}},
            {"api_key": "secret"},
            {"model": "client-selected-model"},
            {"providerName": "client-selected-provider"},
            {"article_id": 1, "metadata": {"source": "https://example.com"}},
        )
        for index, forbidden in enumerate(forbidden_payloads):
            payload = {"client_request_id": "forbidden-%d" % index}
            payload.update(forbidden)
            with self.subTest(payload=forbidden):
                status, _, body = self.post_json(payload)
                self.assertEqual(400, status)
                self.assertEqual("forbidden_input", json.loads(body)["error"])
        self.assertEqual([], self.controller.submit_calls)

    def test_json_must_be_small_strict_and_have_an_idempotency_key(self) -> None:
        common = [
            ("Host", self.running.authority),
            ("Origin", self.running.origin),
            ("X-CSRF-Token", CSRF_TOKEN),
            ("Content-Type", "application/json"),
            ("Connection", "close"),
        ]

        duplicate = b'{"client_request_id":"one","article_id":1,"article_id":2}'
        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common + [("Content-Length", str(len(duplicate)))],
            duplicate,
        )
        self.assertEqual(400, status)

        non_finite = b'{"client_request_id":"one","article_id":NaN}'
        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common + [("Content-Length", str(len(non_finite)))],
            non_finite,
        )
        self.assertEqual(400, status)

        missing_id = b'{"article_id":1}'
        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common + [("Content-Length", str(len(missing_id)))],
            missing_id,
        )
        self.assertEqual(400, status)

        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common
            + [
                ("Content-Length", str(len(missing_id))),
                ("Content-Length", str(len(missing_id))),
            ],
            missing_id,
        )
        self.assertEqual(400, status)

        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common
            + [
                ("Transfer-Encoding", "chunked"),
                ("Content-Length", str(len(missing_id))),
            ],
            missing_id,
        )
        self.assertEqual(400, status)

        status, _, _ = self.running.request_with_headers(
            "POST",
            "/api/ai/jobs",
            common + [("Content-Length", "4097")],
        )
        self.assertEqual(413, status)

        status, _, _ = self.running.request(
            "POST",
            "/api/ai/jobs",
            body=b"{}",
            headers={
                "Origin": self.running.origin,
                "X-CSRF-Token": CSRF_TOKEN,
                "Content-Type": "text/plain",
            },
        )
        self.assertEqual(415, status)
        self.assertEqual([], self.controller.submit_calls)

    def test_api_is_404_without_an_enabled_controller_and_static_files_still_work(self) -> None:
        disabled = RunningServer(
            self.root,
            controller=self.controller,
            enable_ai_actions=False,
        )
        missing_controller = RunningServer(
            self.root,
            controller=None,
            enable_ai_actions=True,
        )
        try:
            for running in (disabled, missing_controller):
                with self.subTest(enabled=running is missing_controller):
                    status, _, _ = running.request(
                        "GET",
                        "/api/ai/session",
                        headers={"Origin": running.origin},
                    )
                    self.assertEqual(404, status)
                    static_status, headers, body = running.request("GET", "/index.html")
                    self.assertEqual(200, static_status)
                    self.assertIn(b"Reader", body)
                    self.assert_security_headers(headers)
                    post_status, _, _ = running.request("POST", "/index.html")
                    self.assertEqual(501, post_status)
        finally:
            disabled.close()
            missing_controller.close()
        self.assertEqual(0, self.controller.session_calls)

    def test_ai_index_is_rendered_in_memory_and_cannot_be_overwritten_by_static_render(self) -> None:
        dynamic = RunningServer(
            self.root,
            controller=self.controller,
            enable_ai_actions=True,
            index_renderer=lambda: "<!doctype html><title>AI controls</title>",
        )
        try:
            status, headers, body = dynamic.request("GET", "/index.html")
            self.assertEqual(200, status)
            self.assertIn(b"AI controls", body)
            self.assertEqual(str(len(body)), headers["Content-Length"])
            (self.root / "index.html").write_text(
                "<!doctype html><title>Static overwrite</title>", encoding="utf-8"
            )
            status, _, body = dynamic.request("GET", "/")
            self.assertEqual(200, status)
            self.assertIn(b"AI controls", body)
            self.assertNotIn(b"Static overwrite", body)
            status, headers, body = dynamic.request("HEAD", "/index.html")
            self.assertEqual(200, status)
            self.assertEqual(b"", body)
            self.assertGreater(int(headers["Content-Length"]), 0)
        finally:
            dynamic.close()

    def test_ai_routes_reject_queries_unknown_paths_and_non_integer_ids(self) -> None:
        for path in (
            "/api/ai/session?token=please",
            "/api/ai/jobs/not-an-int",
            "/api/ai/jobs/-1",
            "/api/ai/unknown",
        ):
            with self.subTest(path=path):
                status, _, _ = self.running.request(
                    "GET", path, headers=self.api_headers()
                )
                self.assertEqual(404, status)

    def test_options_make_ai_actions_loopback_only_and_incompatible_with_network(self) -> None:
        self.assertEqual(
            "127.0.0.1",
            validate_server_options(
                "127.0.0.1", 8765, enable_ai_actions=True
            ),
        )
        with self.assertRaisesRegex(ValueError, "loopback-only.*--allow-network"):
            validate_server_options(
                "127.0.0.1",
                8765,
                allow_network=True,
                enable_ai_actions=True,
            )
        with self.assertRaisesRegex(ValueError, "--allow-network"):
            validate_server_options("0.0.0.0", 8765, enable_ai_actions=True)

    def test_serve_generates_one_startup_csrf_secret_only_when_enabled(self) -> None:
        instances = []

        class InterruptingServer:
            def __init__(self, address, handler) -> None:
                self.server_address = address
                self.handler = handler
                self.closed = False
                instances.append(self)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        with mock.patch(
            "aaron_reader.server._server_class_for_host",
            return_value=InterruptingServer,
        ), mock.patch(
            "aaron_reader.server.secrets.token_urlsafe",
            return_value="startup-csrf",
        ) as token_urlsafe, redirect_stdout(io.StringIO()):
            serve(
                self.root,
                "127.0.0.1",
                8765,
                enable_ai_actions=True,
                controller=self.controller,
            )

        token_urlsafe.assert_called_once_with(32)
        self.assertEqual("startup-csrf", instances[0].handler.keywords["csrf_token"])
        self.assertIs(self.controller, instances[0].handler.keywords["controller"])
        self.assertTrue(instances[0].handler.keywords["enable_ai_actions"])
        self.assertTrue(instances[0].closed)


if __name__ == "__main__":
    unittest.main()
