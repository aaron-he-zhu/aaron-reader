import contextlib
import functools
import http.client
import io
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.cli import build_parser, main  # noqa: E402
from aaron_reader.models import AppConfig  # noqa: E402
from aaron_reader.server import (  # noqa: E402
    ReaderHTTPServer,
    ReaderRequestHandler,
    format_server_url,
    serve as serve_reader,
    validate_server_options,
)


class QuietReaderRequestHandler(ReaderRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class ServerOptionTests(unittest.TestCase):
    def test_loopback_is_default_and_network_requires_opt_in(self) -> None:
        for host in ("127.0.0.1", "127.42.0.1", "::1", "[::1]", "localhost", "LOCALHOST."):
            with self.subTest(host=host):
                validate_server_options(host, 8765)

        for host in ("", "0.0.0.0", "::", "192.168.1.20", "reader.example"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "--allow-network"):
                    validate_server_options(host, 8765)
                validate_server_options(host, 8765, allow_network=True)

    def test_port_range_is_enforced_by_server_and_cli(self) -> None:
        self.assertEqual("127.0.0.1", validate_server_options("127.0.0.1", 1))
        self.assertEqual("127.0.0.1", validate_server_options("127.0.0.1", 65535))
        for port in (0, -1, 65536, True):
            with self.subTest(port=port):
                with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
                    validate_server_options("127.0.0.1", port)  # type: ignore[arg-type]

        parser = build_parser()
        for value in ("0", "-1", "65536", "not-a-port"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["serve", "--port", value])

        args = parser.parse_args(
            ["serve", "--host", "0.0.0.0", "--port", "8080", "--allow-network"]
        )
        self.assertEqual(8080, args.port)
        self.assertTrue(args.allow_network)

    def test_ipv6_url_uses_brackets(self) -> None:
        self.assertEqual("http://[::1]:8765/", format_server_url("::1", 8765))
        self.assertEqual("http://[::1]:8765/", format_server_url("[::1]", 8765))
        self.assertEqual("http://127.0.0.1:8765/", format_server_url("127.0.0.1", 8765))

    def test_simplified_chinese_validation_errors_are_available(self) -> None:
        with self.assertRaisesRegex(ValueError, "端口必须在 1 到 65535 之间"):
            validate_server_options("127.0.0.1", 0, language="zh-CN")
        with self.assertRaisesRegex(ValueError, "默认只允许监听 loopback"):
            validate_server_options("0.0.0.0", 8765, language="zh-CN")
        with self.assertRaisesRegex(ValueError, "监听地址必须是字符串"):
            validate_server_options(123, 8765, language="zh-CN")  # type: ignore[arg-type]

    def test_serve_prints_simplified_chinese_lifecycle_messages(self) -> None:
        class InterruptingServer:
            def __init__(self, address, handler) -> None:
                del handler
                self.server_address = address
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        output = io.StringIO()
        with mock.patch(
            "aaron_reader.server._server_class_for_host",
            return_value=InterruptingServer,
        ), contextlib.redirect_stdout(output):
            serve_reader(
                Path("public"),
                "127.0.0.1",
                8765,
                language="zh-CN",
            )

        self.assertIn("Aaron Reader 已启动：http://127.0.0.1:8765/", output.getvalue())
        self.assertIn("按 Ctrl-C 停止。", output.getvalue())
        self.assertIn("已停止。", output.getvalue())

    @mock.patch("aaron_reader.cli.serve")
    @mock.patch("aaron_reader.cli.render_outputs")
    @mock.patch("aaron_reader.cli.Database")
    @mock.patch("aaron_reader.cli.load_config", return_value=AppConfig(sources=[]))
    def test_cli_rejects_network_bind_before_render(
        self,
        load_config: mock.Mock,
        database: mock.Mock,
        render_outputs: mock.Mock,
        serve: mock.Mock,
    ) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            result = main(["serve", "--host", "0.0.0.0"])
        self.assertEqual(2, result)
        load_config.assert_not_called()
        database.assert_not_called()
        render_outputs.assert_not_called()
        serve.assert_not_called()

    @mock.patch("aaron_reader.cli.serve")
    @mock.patch("aaron_reader.cli.render_outputs")
    @mock.patch("aaron_reader.cli.Database")
    @mock.patch("aaron_reader.cli.load_config", return_value=AppConfig(sources=[]))
    def test_cli_passes_explicit_network_opt_in_to_server(
        self,
        load_config: mock.Mock,
        database: mock.Mock,
        render_outputs: mock.Mock,
        serve: mock.Mock,
    ) -> None:
        del load_config, database
        result = main(
            ["serve", "--host", "0.0.0.0", "--port", "8080", "--allow-network"]
        )
        self.assertEqual(0, result)
        render_outputs.assert_called_once()
        serve.assert_called_once()
        positional, keyword = serve.call_args
        self.assertEqual("0.0.0.0", positional[1])
        self.assertEqual(8080, positional[2])
        self.assertTrue(keyword["allow_network"])


class StaticFileServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.contents = {
            "index.html": b"<!doctype html><title>Reader</title>",
            "latest.json": b'{"ok":true}\n',
            "feed.xml": b"<rss></rss>\n",
            "digest.md": b"# Digest\n",
        }
        for name, content in self.contents.items():
            (self.root / name).write_bytes(content)
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        (self.root / "subdirectory").mkdir()
        (self.root / "subdirectory" / "index.html").write_text("nested", encoding="utf-8")

        handler = functools.partial(QuietReaderRequestHandler, directory=str(self.root))
        self.server = ReaderHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path: str, method: str = "GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, headers, body

    def assert_security_headers(self, headers) -> None:
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Resource-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Opener-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("camera=()", headers["Permissions-Policy"])

    def test_only_allowlisted_artifacts_are_served(self) -> None:
        expected = {
            "/": (self.contents["index.html"], "text/html; charset=utf-8"),
            "/index.html": (self.contents["index.html"], "text/html; charset=utf-8"),
            "/latest.json?cache=no": (
                self.contents["latest.json"],
                "application/json; charset=utf-8",
            ),
            "/feed.xml": (self.contents["feed.xml"], "application/rss+xml; charset=utf-8"),
            "/digest.md": (self.contents["digest.md"], "text/markdown; charset=utf-8"),
        }
        for path, (expected_body, expected_type) in expected.items():
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(200, status)
                self.assertEqual(expected_body, body)
                self.assertEqual(expected_type, headers["Content-Type"])
                self.assertEqual(str(len(expected_body)), headers["Content-Length"])
                self.assertEqual("AaronReader", headers["Server"])
                self.assert_security_headers(headers)

    def test_directories_traversal_and_unlisted_files_are_not_served(self) -> None:
        paths = (
            "/secret.txt",
            "/subdirectory/",
            "/subdirectory/index.html",
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/index.html/",
            "/favicon.ico",
            "//outside.example/index.html",
        )
        for path in paths:
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(404, status)
                self.assertNotIn(b"secret", body)
                self.assert_security_headers(headers)

    def test_allowed_name_is_rejected_when_it_is_a_symlink_or_directory(self) -> None:
        latest = self.root / "latest.json"
        latest.unlink()
        try:
            os.symlink(self.root / "secret.txt", latest)
        except (AttributeError, NotImplementedError, OSError) as exc:
            self.skipTest("symlinks are unavailable: %s" % exc)
        status, headers, body = self.request("/latest.json")
        self.assertEqual(404, status)
        self.assertNotIn(b"secret", body)
        self.assert_security_headers(headers)

        latest.unlink()
        latest.mkdir()
        status, _, _ = self.request("/latest.json")
        self.assertEqual(404, status)

    def test_head_has_metadata_but_no_body(self) -> None:
        status, headers, body = self.request("/feed.xml", method="HEAD")
        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertEqual(str(len(self.contents["feed.xml"])), headers["Content-Length"])
        self.assert_security_headers(headers)


if __name__ == "__main__":
    unittest.main()
