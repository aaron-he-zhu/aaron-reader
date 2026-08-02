import functools
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Mapping, Optional, Tuple, Type
from urllib.parse import urlsplit

from .i18n import translate


_ALLOWED_FILES: Dict[str, Tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/latest.json": ("latest.json", "application/json; charset=utf-8"),
    "/feed.xml": ("feed.xml", "application/rss+xml; charset=utf-8"),
    "/digest.md": ("digest.md", "text/markdown; charset=utf-8"),
}

_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "connect-src 'self'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

_AI_SESSION_PATH = "/api/ai/session"
_AI_JOBS_PATH = "/api/ai/jobs"
_AI_JOB_PATH = re.compile(r"^/api/ai/jobs/([0-9]+)$")
_MAX_AI_REQUEST_BYTES = 4096
_MAX_CLIENT_REQUEST_ID_LENGTH = 128
_MAX_DYNAMIC_INDEX_BYTES = 16 * 1024 * 1024
_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FORBIDDEN_AI_FIELD_PARTS = frozenset(
    (
        "authorization",
        "endpoint",
        "instruction",
        "instructions",
        "key",
        "model",
        "prompt",
        "provider",
        "secret",
        "token",
        "uri",
        "url",
    )
)


class _DuplicateJSONKey(ValueError):
    pass


def _normalize_host(host: str, language: str = "en") -> str:
    if not isinstance(host, str):
        raise ValueError(translate("server.host_string", language))
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if any(character in normalized for character in ("/", "?", "#", "\x00")):
        raise ValueError(translate("server.invalid_host", language, host=host))
    return normalized


def _is_loopback_address(host: str) -> bool:
    if not host:
        return False
    address_text = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _resolve_localhost(host: str, language: str = "en") -> str:
    try:
        addresses = [
            item[4][0]
            for item in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ]
    except socket.gaierror as exc:
        raise ValueError(
            translate("server.resolve_failed", language, host=host, error=exc)
        ) from exc
    if not addresses or not all(_is_loopback_address(address) for address in addresses):
        raise ValueError(translate("server.localhost_not_loopback", language))
    return addresses[0]


def validate_server_options(
    host: str,
    port: int,
    allow_network: bool = False,
    language: str = "en",
    enable_ai_actions: bool = False,
) -> str:
    """Validate a bind target and return its normalized host spelling.

    Hostnames other than ``localhost`` require an explicit network opt-in.  This
    deliberately avoids treating an arbitrary, mutable DNS result as proof that
    a requested bind is local-only.
    """

    normalized_host = _normalize_host(host, language=language)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(translate("server.port_range", language))
    if enable_ai_actions and allow_network:
        raise ValueError(
            "AI actions are loopback-only and cannot be combined with --allow-network"
        )
    if not allow_network:
        if normalized_host.rstrip(".").lower() == "localhost":
            return _resolve_localhost(normalized_host, language=language)
        if not _is_loopback_address(normalized_host):
            raise ValueError(translate("server.network_opt_in", language))
    return normalized_host


def format_server_url(host: str, port: int, language: str = "en") -> str:
    normalized_host = _normalize_host(host, language=language) or "0.0.0.0"
    display_host = normalized_host
    if ":" in display_host:
        display_host = "[%s]" % display_host.replace("%", "%25")
    return "http://%s:%d/" % (display_host, port)


class ReaderRequestHandler(BaseHTTPRequestHandler):
    """Serve generated artifacts and, when explicitly enabled, local AI actions."""

    server_version = "AaronReader"
    sys_version = ""

    def __init__(
        self,
        *args: object,
        directory: Optional[str] = None,
        controller: Optional[object] = None,
        index_renderer: Optional[Callable[[], str]] = None,
        csrf_token: str = "",
        enable_ai_actions: bool = False,
        **kwargs: object,
    ) -> None:
        self.directory = Path(directory or os.getcwd()).resolve()
        self.ai_controller = controller if enable_ai_actions else None
        self.ai_index_renderer = index_renderer if enable_ai_actions else None
        self.ai_csrf_token = csrf_token if enable_ai_actions else ""
        self.ai_actions_enabled = bool(enable_ai_actions)
        super().__init__(*args, **kwargs)

    def version_string(self) -> str:
        return self.server_version

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        super().end_headers()

    def do_GET(self) -> None:
        target = self._request_target()
        if target is not None and self._is_ai_path(target.path):
            self._serve_ai_get(target.path, target.query)
            return
        self._serve_allowed_file(send_body=True)

    def do_HEAD(self) -> None:
        self._serve_allowed_file(send_body=False)

    def do_POST(self) -> None:
        target = self._request_target()
        if target is not None and self._is_ai_path(target.path):
            self._serve_ai_post(target.path, target.query)
            return
        self.send_error(501, "Unsupported method ('POST')")

    def _request_target(self):
        try:
            target = urlsplit(self.path)
        except ValueError:
            return None
        if target.scheme or target.netloc or target.fragment:
            return None
        return target

    @staticmethod
    def _is_ai_path(path: str) -> bool:
        return path == "/api/ai" or path.startswith("/api/ai/")

    def _serve_ai_get(self, path: str, query: str) -> None:
        controller = self.ai_controller
        if controller is None or query:
            self._send_ai_error(404, "not_found")
            return

        if path == _AI_SESSION_PATH:
            if not self._authorize_ai_request(
                require_csrf=False,
                require_origin=False,
            ):
                return
            try:
                session_method = getattr(controller, "session")
                if not callable(session_method):
                    raise TypeError("controller.session is not callable")
                session = session_method()
                if session is None:
                    response: Dict[str, object] = {}
                elif isinstance(session, Mapping):
                    response = dict(session)
                else:
                    response = {"session": session}
                response["csrf_token"] = self.ai_csrf_token
            except Exception:
                self._send_ai_error(500, "controller_error")
                return
            self._send_json(200, response)
            return

        match = _AI_JOB_PATH.fullmatch(path)
        if match is None:
            self._send_ai_error(404, "not_found")
            return
        if not self._authorize_ai_request(
            require_csrf=True,
            require_origin=False,
        ):
            return
        try:
            job_id = int(match.group(1))
        except (TypeError, ValueError, OverflowError):
            self._send_ai_error(404, "not_found")
            return
        if job_id > 9_223_372_036_854_775_807:
            self._send_ai_error(404, "not_found")
            return
        try:
            job_method = getattr(controller, "job")
            if not callable(job_method):
                raise TypeError("controller.job is not callable")
            result = job_method(job_id)
        except Exception:
            self._send_ai_error(500, "controller_error")
            return
        if result is None:
            self._send_ai_error(404, "not_found")
            return
        self._send_controller_result(200, result)

    def _serve_ai_post(self, path: str, query: str) -> None:
        controller = self.ai_controller
        if controller is None or query or path != _AI_JOBS_PATH:
            self._send_ai_error(404, "not_found")
            return
        if not self._authorize_ai_request(
            require_csrf=True,
            require_origin=True,
        ):
            return

        request = self._read_ai_request()
        if request is None:
            return
        payload, client_request_id = request
        try:
            submit_method = getattr(controller, "submit")
            if not callable(submit_method):
                raise TypeError("controller.submit is not callable")
            result = submit_method(payload, client_request_id)
        except ValueError:
            self._send_ai_error(400, "invalid_request")
            return
        except Exception:
            self._send_ai_error(500, "controller_error")
            return
        self._send_controller_result(202, result)

    def _authorize_ai_request(
        self,
        require_csrf: bool,
        require_origin: bool,
    ) -> bool:
        client_host = str(self.client_address[0]) if self.client_address else ""
        server_address = getattr(self.server, "server_address", ("", 0))
        server_host = str(server_address[0]) if server_address else ""
        if not _is_loopback_address(client_host) or not _is_loopback_address(server_host):
            self._send_ai_error(403, "forbidden")
            return False

        origin = self._expected_ai_origin()
        expected_host = urlsplit(origin).netloc
        hosts = self._header_values("Host")
        origins = self._header_values("Origin")
        if hosts != [expected_host]:
            self._send_ai_error(403, "forbidden")
            return False
        if (require_origin and origins != [origin]) or (
            not require_origin and origins not in ([], [origin])
        ):
            self._send_ai_error(403, "forbidden")
            return False

        if require_csrf:
            csrf_values = self._header_values("X-CSRF-Token")
            if (
                len(csrf_values) != 1
                or not self.ai_csrf_token
                or not secrets.compare_digest(csrf_values[0], self.ai_csrf_token)
            ):
                self._send_ai_error(403, "forbidden")
                return False
        return True

    def _expected_ai_origin(self) -> str:
        server_address = getattr(self.server, "server_address", ("", 0))
        host = str(server_address[0])
        port = int(server_address[1])
        return format_server_url(host, port).rstrip("/")

    def _header_values(self, name: str) -> List[str]:
        return [
            str(value).strip()
            for value in (self.headers.get_all(name, []) or [])
        ]

    def _read_ai_request(self) -> Optional[Tuple[Dict[str, Any], str]]:
        if self._header_values("Transfer-Encoding"):
            self._send_ai_error(400, "transfer_encoding_not_allowed")
            return None

        content_types = self._header_values("Content-Type")
        if len(content_types) != 1 or not self._is_json_content_type(content_types[0]):
            self._send_ai_error(415, "application_json_required")
            return None

        content_lengths = self._header_values("Content-Length")
        if not content_lengths:
            self._send_ai_error(411, "content_length_required")
            return None
        if len(content_lengths) != 1 or not content_lengths[0].isdigit():
            self._send_ai_error(400, "invalid_content_length")
            return None
        content_length = int(content_lengths[0])
        if content_length > _MAX_AI_REQUEST_BYTES:
            self._send_ai_error(413, "request_too_large")
            return None
        if content_length <= 0:
            self._send_ai_error(400, "invalid_json")
            return None

        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self._send_ai_error(400, "incomplete_body")
            return None
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=self._unique_json_object,
                parse_constant=self._reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKey, ValueError):
            self._send_ai_error(400, "invalid_json")
            return None
        if not isinstance(payload, dict):
            self._send_ai_error(400, "json_object_required")
            return None

        mutable_payload: Dict[str, Any] = dict(payload)
        try:
            client_request_id = self._extract_client_request_id(mutable_payload)
        except ValueError:
            self._send_ai_error(400, "invalid_client_request_id")
            return None
        if self._contains_forbidden_ai_input(mutable_payload):
            self._send_ai_error(400, "forbidden_input")
            return None
        return mutable_payload, client_request_id

    @staticmethod
    def _is_json_content_type(value: str) -> bool:
        parts = [part.strip() for part in value.split(";")]
        if not parts or parts[0].lower() != "application/json":
            return False
        charset_seen = False
        for parameter in parts[1:]:
            if "=" not in parameter:
                return False
            name, configured = parameter.split("=", 1)
            if name.strip().lower() != "charset" or charset_seen:
                return False
            charset_seen = True
            charset = configured.strip().strip('"').lower().replace("_", "-")
            if charset not in ("utf-8", "utf8"):
                return False
        return True

    @staticmethod
    def _unique_json_object(pairs):
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJSONKey(key)
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError("non-finite JSON number: %s" % value)

    def _extract_client_request_id(self, payload: Dict[str, Any]) -> str:
        candidates: List[object] = []
        if "client_request_id" in payload:
            candidates.append(payload.pop("client_request_id"))
        for header in ("X-Client-Request-ID", "Idempotency-Key"):
            values = self._header_values(header)
            if len(values) > 1:
                raise ValueError("duplicate client request ID")
            if values:
                candidates.append(values[0])
        if not candidates or any(not isinstance(value, str) for value in candidates):
            raise ValueError("client_request_id is required")
        identifiers = [str(value).strip() for value in candidates]
        client_request_id = identifiers[0]
        if any(value != client_request_id for value in identifiers[1:]):
            raise ValueError("conflicting client request IDs")
        if (
            not client_request_id
            or len(client_request_id) > _MAX_CLIENT_REQUEST_ID_LENGTH
            or _CLIENT_REQUEST_ID.fullmatch(client_request_id) is None
        ):
            raise ValueError("invalid client_request_id")
        return client_request_id

    @classmethod
    def _contains_forbidden_ai_input(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if cls._forbidden_ai_field(str(key)):
                    return True
                if cls._contains_forbidden_ai_input(nested):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(cls._contains_forbidden_ai_input(item) for item in value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            return bool(
                normalized.startswith(("//", "data:", "file:", "javascript:"))
                or re.match(r"^[a-z][a-z0-9+.-]*://", normalized)
            )
        return False

    @staticmethod
    def _forbidden_ai_field(value: str) -> bool:
        separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        parts = {
            part.lower()
            for part in re.split(r"[^A-Za-z0-9]+", separated)
            if part
        }
        return bool(parts & _FORBIDDEN_AI_FIELD_PARTS)

    def _send_controller_result(self, status: int, result: object) -> None:
        if result is None:
            payload: object = {}
        elif isinstance(result, Mapping):
            payload = dict(result)
        else:
            payload = {"result": result}
        self._send_json(status, payload)

    def _send_ai_error(self, status: int, code: str) -> None:
        self._send_json(status, {"error": code})

    def _send_json(self, status: int, payload: object) -> None:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = b'{"error":"controller_error"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_file(self) -> Optional[Tuple[str, str]]:
        target = self._request_target()
        if target is None:
            return None
        return _ALLOWED_FILES.get(target.path)

    def _open_regular_file(self, filename: str) -> Optional[Tuple[BinaryIO, os.stat_result]]:
        candidate = self.directory / filename
        try:
            before = candidate.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(before.st_mode):
            return None

        flags = os.O_RDONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(candidate), flags)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                return None
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(descriptor)
                return None
            return os.fdopen(descriptor, "rb"), opened
        except (OSError, ValueError):
            try:
                os.close(descriptor)
            except OSError:
                pass
            return None

    def _serve_allowed_file(self, send_body: bool) -> None:
        requested = self._request_file()
        if requested is None:
            self.send_error(404, "Not Found")
            return
        filename, content_type = requested
        if filename == "index.html" and self.ai_index_renderer is not None:
            try:
                rendered = self.ai_index_renderer()
                if not isinstance(rendered, str):
                    raise TypeError("index renderer must return text")
                body = rendered.encode("utf-8")
                if len(body) > _MAX_DYNAMIC_INDEX_BYTES:
                    raise ValueError("dynamic index exceeds byte limit")
            except Exception:
                self.send_error(500, "Could not render index")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return
        opened = self._open_regular_file(filename)
        if opened is None:
            self.send_error(404, "Not Found")
            return
        handle, file_status = opened
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_status.st_size))
            self.send_header("Last-Modified", self.date_time_string(file_status.st_mtime))
            self.end_headers()
            if send_body:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            handle.close()

    def log_message(self, format: str, *args: object) -> None:
        print("[web] %s" % (format % args))


class ReaderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class IPv6ReaderHTTPServer(ReaderHTTPServer):
    address_family = socket.AF_INET6


def _server_class_for_host(host: str) -> Type[ReaderHTTPServer]:
    address_text = host.split("%", 1)[0]
    try:
        if ipaddress.ip_address(address_text).version == 6:
            return IPv6ReaderHTTPServer
    except ValueError:
        pass
    return ReaderHTTPServer


def serve(
    output_dir: Path,
    host: str,
    port: int,
    open_browser: bool = False,
    allow_network: bool = False,
    language: str = "en",
    enable_ai_actions: bool = False,
    controller: Optional[object] = None,
    index_renderer: Optional[Callable[[], str]] = None,
) -> None:
    normalized_host = validate_server_options(
        host,
        port,
        allow_network=allow_network,
        language=language,
        enable_ai_actions=enable_ai_actions,
    )
    csrf_token = secrets.token_urlsafe(32) if enable_ai_actions else ""
    handler = functools.partial(
        ReaderRequestHandler,
        directory=str(output_dir),
        controller=controller,
        index_renderer=index_renderer,
        csrf_token=csrf_token,
        enable_ai_actions=enable_ai_actions,
    )
    server_class = _server_class_for_host(normalized_host)
    server = server_class((normalized_host, port), handler)
    url = format_server_url(
        normalized_host,
        int(server.server_address[1]),
        language=language,
    )
    print(translate("server.started", language, url=url))
    print(translate("server.stop_hint", language))
    if open_browser:
        timer = threading.Timer(0.35, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n%s" % translate("server.stopped", language))
    finally:
        server.server_close()
