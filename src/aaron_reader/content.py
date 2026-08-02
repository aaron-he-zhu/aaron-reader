"""Secure, deterministic article-content acquisition.

This module is deliberately independent from feed synchronization and from any
AI provider.  It retrieves a single explicitly allowlisted public HTML page,
extracts normalized main text, and returns a provenance-rich snapshot that can
be reviewed or budgeted before any optional downstream model call.

The URL checks are intentionally strict.  Every redirect is followed manually
and revalidated, all DNS answers must be globally routable, credentials and
non-standard ports are rejected, and the default opener does not use ambient
proxy, cookie, or authentication handlers.
"""

import hashlib
import ipaddress
import math
import re
import socket
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit


CONTENT_EXTRACTOR_VERSION = "stdlib-main-text-v1"
CONTENT_USER_AGENT = "AaronReader/1.1 (deterministic article content fetcher)"

_HTML_CONTENT_TYPES = frozenset(("text/html", "application/xhtml+xml"))
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_DEFAULT_PORTS = {"http": 80, "https": 443}
_VOID_TAGS = frozenset(
    (
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    )
)
_BLOCK_TAGS = frozenset(
    (
        "address",
        "article",
        "blockquote",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    )
)
_SUPPRESSED_TAGS = frozenset(
    (
        "applet",
        "aside",
        "audio",
        "button",
        "canvas",
        "dialog",
        "embed",
        "footer",
        "form",
        "iframe",
        "input",
        "nav",
        "noscript",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "template",
        "textarea",
        "video",
    )
)
_NEGATIVE_ATTRIBUTE_TOKENS = frozenset(
    (
        "ad",
        "ads",
        "advert",
        "advertisement",
        "breadcrumb",
        "comments",
        "cookie",
        "footer",
        "menu",
        "nav",
        "navigation",
        "newsletter",
        "popup",
        "promo",
        "related",
        "share",
        "sidebar",
        "social",
        "subscribe",
    )
)
_CONTENT_ATTRIBUTE_VALUES = frozenset(
    (
        "article-body",
        "article-content",
        "entry-content",
        "main-content",
        "post-body",
        "post-content",
        "story-body",
        "story-content",
    )
)
_CHARSET_PATTERN = re.compile(
    r"charset\s*=\s*[\"']?\s*([A-Za-z0-9._:+-]+)", re.IGNORECASE
)
_META_CHARSET_PATTERN = re.compile(
    br"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._:+-]+)",
    re.IGNORECASE,
)


class ContentError(RuntimeError):
    """Base class for content acquisition and extraction failures."""


class ContentSecurityError(ContentError):
    """Raised when a URL, redirect, or resolved address violates policy."""


class ContentFetchError(ContentError):
    """Raised when a permitted page cannot be safely retrieved or decoded."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ContentSnapshot:
    """Immutable provenance for normalized article text.

    ``full_text_sha256`` identifies all normalized extracted text before the
    caller's character limit.  ``text_sha256`` identifies the exact retained
    text suitable for a later prompt.  Keeping both prevents a truncated input
    from being confused with a complete article version.
    """

    source_url: str
    final_url: str
    fetched_at: str
    status: int
    content_type: str
    charset: str
    etag: str
    last_modified: str
    title: str
    text: str
    source_body_sha256: str
    full_text_sha256: str
    text_sha256: str
    original_character_count: int
    character_count: int
    utf8_bytes: int
    truncated: bool
    extractor_version: str = CONTENT_EXTRACTOR_VERSION


@dataclass
class _Node:
    tag: str
    attributes: Dict[str, str] = field(default_factory=dict)
    children: List[object] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Build the small tree needed for deterministic candidate selection."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self._stack: List[_Node] = [self.root]

    def handle_starttag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        lowered = tag.lower()
        attributes = {
            str(key).lower(): str(value or "")
            for key, value in attrs
            if key
        }
        node = _Node(lowered, attributes)
        self._stack[-1].children.append(node)
        if lowered not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        lowered = tag.lower()
        attributes = {
            str(key).lower(): str(value or "")
            for key, value in attrs
            if key
        }
        self._stack[-1].children.append(_Node(lowered, attributes))

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == lowered:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return redirect responses to the caller instead of following them."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def normalize_content_text(value: str) -> str:
    """Normalize extracted prose while retaining paragraph boundaries."""

    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u2028", "\n").replace("\u2029", "\n")
    cleaned: List[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\n":
            cleaned.append(character)
        elif character == "\t" or character == "\u00a0":
            cleaned.append(" ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            cleaned.append(" ")
        else:
            cleaned.append(character)
    normalized = "".join(cleaned)
    normalized = re.sub(r"[^\S\n]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def truncate_content(text: str, max_characters: int) -> Tuple[str, bool]:
    """Deterministically truncate text without exceeding the character cap."""

    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise ValueError("max_characters must be a positive integer")
    if max_characters < 1:
        raise ValueError("max_characters must be a positive integer")
    if len(text) <= max_characters:
        return text, False
    if max_characters == 1:
        return "…", True

    available = max_characters - 1
    prefix = text[:available]
    minimum_boundary = max(1, available // 2)
    boundaries = [
        prefix.rfind("\n\n"),
        prefix.rfind("\n"),
        prefix.rfind(" "),
        prefix.rfind("。"),
        prefix.rfind("！"),
        prefix.rfind("？"),
        prefix.rfind(". "),
        prefix.rfind("! "),
        prefix.rfind("? "),
    ]
    boundary = max(boundaries)
    if boundary >= minimum_boundary:
        punctuation_width = (
            1 if prefix[boundary : boundary + 1] in ".!?。！？" else 0
        )
        prefix = prefix[: boundary + punctuation_width]
    retained = prefix.rstrip()
    if not retained:
        retained = text[:available]
    return retained + "…", True


def extract_main_content(html_text: str) -> Tuple[str, str]:
    """Return ``(title, normalized main text)`` from an HTML document."""

    parser = _DocumentParser()
    try:
        parser.feed(html_text)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ContentFetchError("invalid HTML document: %s" % exc) from exc

    title = ""
    title_nodes = [node for node in _walk_nodes(parser.root) if node.tag == "title"]
    if title_nodes:
        title = normalize_content_text(_text_from_node(title_nodes[0])).replace("\n", " ")

    all_nodes = list(_walk_nodes(parser.root))
    candidate_groups = (
        [node for node in all_nodes if node.tag == "article"],
        [node for node in all_nodes if node.tag == "main"],
        [node for node in all_nodes if _is_content_candidate(node)],
        [node for node in all_nodes if node.tag == "body"],
        [parser.root],
    )
    for candidates in candidate_groups:
        scored: List[Tuple[int, int, str]] = []
        for index, candidate in enumerate(candidates):
            text = normalize_content_text(_text_from_node(candidate))
            if text:
                scored.append((len(text), -index, text))
        if scored:
            return title, max(scored)[2]
    return title, ""


def validate_public_url(
    url: str,
    allowed_hosts: Iterable[str],
    resolver: Optional[Callable[..., object]] = None,
) -> str:
    """Validate an HTTP(S) URL and return its normalized, fragment-free form."""

    normalized_hosts = frozenset(_normalize_allowed_host(item) for item in allowed_hosts)
    if not normalized_hosts:
        raise ContentSecurityError("allowed_hosts must contain at least one host")
    normalized_url, hostname, port = _normalize_fetch_url(url)
    if hostname not in normalized_hosts:
        raise ContentSecurityError("URL host is not allowlisted: %s" % hostname)
    _require_public_addresses(hostname, port, resolver=resolver)
    return normalized_url


class ContentFetcher:
    """Fetch an allowlisted public HTML article and create a text snapshot."""

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        max_characters: int = 100_000,
        max_redirects: int = 5,
        resolver: Optional[Callable[..., object]] = None,
        opener: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.allowed_hosts = frozenset(
            _normalize_allowed_host(item) for item in allowed_hosts
        )
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts must contain at least one host")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        if (
            isinstance(max_characters, bool)
            or not isinstance(max_characters, int)
            or max_characters < 1
        ):
            raise ValueError("max_characters must be a positive integer")
        if (
            isinstance(max_redirects, bool)
            or not isinstance(max_redirects, int)
            or not 0 <= max_redirects <= 20
        ):
            raise ValueError("max_redirects must be between 0 and 20")

        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self.max_characters = max_characters
        self.max_redirects = max_redirects
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def fetch(self, url: str) -> ContentSnapshot:
        source_url = self._validate(url)
        current_url = source_url
        visited = set()
        redirect_count = 0

        while True:
            # DNS is intentionally checked immediately before every hop rather
            # than only once for the original article URL.
            current_url = self._validate(current_url)
            if current_url in visited:
                raise ContentSecurityError("redirect loop detected at %s" % current_url)
            visited.add(current_url)

            request = urllib.request.Request(
                current_url,
                headers={
                    "User-Agent": CONTENT_USER_AGENT,
                    "Accept": "text/html, application/xhtml+xml;q=0.9",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                },
                method="GET",
            )
            response: Optional[object] = None
            try:
                try:
                    response = self._opener.open(request, timeout=self.timeout_seconds)
                except urllib.error.HTTPError as exc:
                    response = exc
                    if int(exc.code) not in _REDIRECT_STATUSES:
                        raise ContentFetchError(
                            "HTTP %s fetching %s" % (exc.code, current_url),
                            status=int(exc.code),
                        ) from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    raise ContentFetchError(
                        "network error fetching %s: %s" % (current_url, exc)
                    ) from exc

                status = _response_status(response)
                headers = getattr(response, "headers", {})
                response_url = _response_url(response, current_url)
                normalized_response_url, _, _ = _normalize_fetch_url(response_url)
                if normalized_response_url != current_url:
                    raise ContentSecurityError(
                        "opener followed an unvalidated redirect from %s to %s"
                        % (current_url, normalized_response_url)
                    )

                if status in _REDIRECT_STATUSES:
                    location = _header(headers, "Location")
                    if not location:
                        raise ContentFetchError(
                            "HTTP %s response has no Location header" % status,
                            status=status,
                        )
                    if redirect_count >= self.max_redirects:
                        raise ContentSecurityError(
                            "redirect limit of %d exceeded" % self.max_redirects
                        )
                    redirect_count += 1
                    current_url = self._validate(urljoin(current_url, location))
                    continue

                if status != 200:
                    raise ContentFetchError(
                        "unexpected HTTP status %s fetching %s" % (status, current_url),
                        status=status,
                    )

                content_type_header = _header(headers, "Content-Type")
                content_type = content_type_header.split(";", 1)[0].strip().lower()
                if content_type not in _HTML_CONTENT_TYPES:
                    raise ContentFetchError(
                        "unsupported content type %r for %s"
                        % (content_type_header or "missing", current_url),
                        status=status,
                    )
                content_encoding = _header(headers, "Content-Encoding").strip().lower()
                if content_encoding not in ("", "identity"):
                    raise ContentFetchError(
                        "unsupported content encoding %r" % content_encoding,
                        status=status,
                    )
                content_length = _header(headers, "Content-Length").strip()
                if content_length:
                    try:
                        announced_size = int(content_length)
                    except ValueError as exc:
                        raise ContentFetchError("invalid Content-Length header") from exc
                    if announced_size < 0:
                        raise ContentFetchError("invalid Content-Length header")
                    if announced_size > self.max_response_bytes:
                        raise ContentFetchError(
                            "response exceeds %d bytes" % self.max_response_bytes,
                            status=status,
                        )

                body = _read_limited(response, self.max_response_bytes)
                charset = _response_charset(content_type_header, body)
                try:
                    html_text = body.decode(charset, errors="replace")
                except LookupError as exc:
                    raise ContentFetchError(
                        "unsupported response charset %r" % charset,
                        status=status,
                    ) from exc
                title, full_text = extract_main_content(html_text)
                if not full_text:
                    raise ContentFetchError(
                        "HTML page contains no extractable article text", status=status
                    )
                retained_text, truncated = truncate_content(
                    full_text, self.max_characters
                )
                fetched_at = _utc_timestamp(self._clock())
                return ContentSnapshot(
                    source_url=source_url,
                    final_url=current_url,
                    fetched_at=fetched_at,
                    status=status,
                    content_type=content_type,
                    charset=charset,
                    etag=_header(headers, "ETag"),
                    last_modified=_header(headers, "Last-Modified"),
                    title=title,
                    text=retained_text,
                    source_body_sha256=_sha256_bytes(body),
                    full_text_sha256=_sha256_text(full_text),
                    text_sha256=_sha256_text(retained_text),
                    original_character_count=len(full_text),
                    character_count=len(retained_text),
                    utf8_bytes=len(retained_text.encode("utf-8")),
                    truncated=truncated,
                )
            finally:
                if response is not None:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()

    def _validate(self, url: str) -> str:
        return validate_public_url(
            url,
            self.allowed_hosts,
            resolver=self._resolver,
        )


def _normalize_allowed_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("allowed host must be a string")
    raw = value.strip().rstrip(".")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if not raw or any(character in raw for character in "/?#@[]\x00"):
        raise ValueError("allowed host must be a bare hostname: %r" % value)
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        if ":" in raw:
            raise ValueError("allowed host must be a bare hostname: %r" % value)
    try:
        normalized = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid allowed host: %r" % value) from exc
    return normalized


def _normalize_fetch_url(url: str) -> Tuple[str, str, int]:
    if not isinstance(url, str):
        raise ContentSecurityError("content URL must be a string")
    if not url or any(ord(character) < 0x20 for character in url):
        raise ContentSecurityError("content URL contains invalid control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ContentSecurityError("invalid content URL: %r" % url) from exc
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ContentSecurityError("content URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ContentSecurityError("content URL must not contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ContentSecurityError("content URL has no hostname")
    if "%" in hostname:
        raise ContentSecurityError("scoped IP addresses are not permitted")
    raw_hostname = hostname.rstrip(".")
    try:
        normalized_host = str(ipaddress.ip_address(raw_hostname))
    except ValueError:
        try:
            normalized_host = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ContentSecurityError("invalid URL hostname: %r" % hostname) from exc
    if not normalized_host:
        raise ContentSecurityError("content URL has no hostname")
    effective_port = port or _DEFAULT_PORTS[scheme]
    if effective_port != _DEFAULT_PORTS[scheme]:
        raise ContentSecurityError("non-standard URL ports are not permitted")
    host_for_netloc = (
        "[%s]" % normalized_host if ":" in normalized_host else normalized_host
    )
    path = parsed.path or "/"
    normalized = urlunsplit((scheme, host_for_netloc, path, parsed.query, ""))
    return normalized, normalized_host, effective_port


def _require_public_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Optional[Callable[..., object]],
) -> None:
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ContentSecurityError("localhost content hosts are not permitted")
    address_texts: List[str] = []
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        lookup = resolver or socket.getaddrinfo
        try:
            answers = lookup(
                hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError) as exc:
            raise ContentSecurityError(
                "cannot resolve content host %s: %s" % (hostname, exc)
            ) from exc
        try:
            for answer in answers:  # type: ignore[union-attr]
                address_texts.append(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError) as exc:
            raise ContentSecurityError(
                "resolver returned invalid addresses for %s" % hostname
            ) from exc
    else:
        address_texts.append(str(literal))

    if not address_texts:
        raise ContentSecurityError("content host has no resolved addresses: %s" % hostname)
    for address_text in address_texts:
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise ContentSecurityError(
                "resolver returned an invalid address for %s: %s"
                % (hostname, address_text)
            ) from exc
        mapped = getattr(address, "ipv4_mapped", None)
        effective = mapped or address
        if not effective.is_global:
            raise ContentSecurityError(
                "content host resolves to a non-public address: %s" % address_text
            )


def _walk_nodes(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk_nodes(child)


def _is_content_candidate(node: _Node) -> bool:
    values = " ".join(
        (node.attributes.get("id", ""), node.attributes.get("class", ""))
    ).lower()
    tokens = frozenset(item for item in re.split(r"\s+", values) if item)
    return bool(tokens.intersection(_CONTENT_ATTRIBUTE_VALUES))


def _is_suppressed(node: _Node) -> bool:
    if node.tag in _SUPPRESSED_TAGS:
        return True
    if "hidden" in node.attributes:
        return True
    if node.attributes.get("aria-hidden", "").strip().lower() == "true":
        return True
    role = node.attributes.get("role", "").strip().lower()
    if role in ("banner", "complementary", "contentinfo", "navigation"):
        return True
    style = re.sub(r"\s+", "", node.attributes.get("style", "").lower())
    if "display:none" in style or "visibility:hidden" in style:
        return True
    values = " ".join(
        (node.attributes.get("id", ""), node.attributes.get("class", ""))
    ).lower()
    tokens = frozenset(
        item for item in re.split(r"[^a-z0-9_-]+", values) if item
    )
    return bool(tokens.intersection(_NEGATIVE_ATTRIBUTE_TOKENS))


def _text_from_node(node: _Node) -> str:
    parts: List[str] = []

    def visit(current: _Node) -> None:
        if _is_suppressed(current):
            return
        if current.tag in _BLOCK_TAGS:
            parts.append("\n\n")
        for child in current.children:
            if isinstance(child, _Node):
                if child.tag == "br":
                    parts.append("\n")
                elif child.tag == "hr":
                    parts.append("\n\n")
                else:
                    visit(child)
            else:
                parts.append(str(child))
        if current.tag in _BLOCK_TAGS:
            parts.append("\n\n")

    visit(node)
    return "".join(parts)


def _response_status(response: object) -> int:
    getcode = getattr(response, "getcode", None)
    status = getcode() if callable(getcode) else getattr(response, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise ContentFetchError("response has no valid HTTP status")
    return status


def _response_url(response: object, fallback: str) -> str:
    geturl = getattr(response, "geturl", None)
    value = geturl() if callable(geturl) else fallback
    return str(value or fallback)


def _header(headers: object, name: str) -> str:
    get = getattr(headers, "get", None)
    if not callable(get):
        return ""
    value = get(name, None)
    if value is None:
        items = getattr(headers, "items", None)
        if callable(items):
            lowered = name.lower()
            for key, candidate in items():
                if str(key).lower() == lowered:
                    value = candidate
                    break
    return str(value or "")


def _read_limited(response: object, maximum: int) -> bytes:
    read = getattr(response, "read", None)
    if not callable(read):
        raise ContentFetchError("response body is not readable")
    body = bytearray()
    while len(body) <= maximum:
        chunk = read(min(64 * 1024, maximum + 1 - len(body)))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ContentFetchError("response body is not bytes")
        body.extend(chunk)
        if len(body) > maximum:
            raise ContentFetchError("response exceeds %d bytes" % maximum)
    return bytes(body)


def _response_charset(content_type: str, body: bytes) -> str:
    matched = _CHARSET_PATTERN.search(content_type)
    if matched:
        return matched.group(1).lower()
    if body.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    meta = _META_CHARSET_PATTERN.search(body[:4096])
    if meta:
        try:
            return meta.group(1).decode("ascii").lower()
        except UnicodeDecodeError:
            pass
    return "utf-8"


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ContentFetchError("clock must return a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))
