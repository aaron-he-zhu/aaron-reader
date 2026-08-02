import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}

_TEXT_WHITESPACE_CONTROLS = {"\t", "\n", "\r"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_control_characters(value: str, preserve_text_whitespace: bool = True) -> str:
    """Replace unsafe C0/C1 controls while retaining ordinary text whitespace.

    Tabs and line endings are useful separators while extracting HTML and
    Markdown.  Every other C0 control, plus DEL and the C1 range, is replaced by
    a space so removing a control cannot accidentally concatenate two words.
    Callers that are handling a non-text value such as a URL can disable the
    whitespace exception.
    """

    if not value:
        return ""
    parts = []
    for character in value:
        codepoint = ord(character)
        is_c0 = codepoint < 0x20
        is_c1 = 0x7F <= codepoint <= 0x9F
        if not (is_c0 or is_c1):
            parts.append(character)
        elif preserve_text_whitespace and character in _TEXT_WHITESPACE_CONTROLS:
            parts.append(character)
        else:
            parts.append(" ")
    return "".join(parts)


def clean_text(value: Optional[str], limit: Optional[int] = None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value))
        text = " ".join(parser.parts)
    except Exception:
        text = html.unescape(value)
    text = strip_control_characters(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def canonicalize_url(value: str, base_url: str = "") -> str:
    absolute = urljoin(base_url, html.unescape(value.strip()))
    parts = urlsplit(absolute)
    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise ValueError("article URL has no hostname: %r" % value)
    port = parts.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = "%s:%s" % (hostname, port)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
            continue
        query_pairs.append((key, item))
    return urlunsplit((scheme, netloc, path, urlencode(query_pairs, doseq=True), ""))


def parse_datetime(value: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    if not value:
        return None
    raw = clean_text(value)
    current = now or datetime.now(timezone.utc)
    parsed: Optional[datetime] = None

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        pass

    if parsed is None:
        iso_value = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(iso_value)
        except ValueError:
            pass

    if parsed is None:
        formats_with_year = (
            "%B %d, %Y",
            "%b %d, %Y",
            "%Y-%m-%d",
            "%Y/%m/%d",
        )
        for date_format in formats_with_year:
            try:
                parsed = datetime.strptime(raw, date_format)
                break
            except ValueError:
                continue

    if parsed is None:
        for date_format in ("%B %d", "%b %d"):
            try:
                partial = datetime.strptime(raw, date_format)
                parsed = partial.replace(year=current.year)
                if parsed.replace(tzinfo=timezone.utc) > current + timedelta(days=7):
                    parsed = parsed.replace(year=current.year - 1)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_hash(*values: Optional[str]) -> str:
    joined = "\x1f".join(value or "" for value in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
