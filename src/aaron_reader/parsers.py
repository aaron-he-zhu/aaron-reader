"""Deterministic feed and listing-page parsers.

The normal sync path deliberately does not use an LLM.  RSS/Atom entries and the
three HTML listing pages are converted into :class:`ArticleCandidate` values by
fixed rules matching the publishers' current markup.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .models import ArticleCandidate, SourceConfig
from .normalize import canonicalize_url, clean_text, parse_datetime, stable_hash


_HTML_MAX_DEPTH = 256
_HTML_MAX_NODES = 150_000
_SUMMARY_LIMIT = 800
_VOID_ELEMENTS = {
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
}
_TEXT_IGNORED_ELEMENTS = {"script", "style", "noscript", "svg"}
_DATE_PATTERN = re.compile(
    r"^(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?$",
    re.IGNORECASE,
)


@dataclass
class _HTMLNode:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    parent: Optional["_HTMLNode"] = None
    content: List[Union[str, "_HTMLNode"]] = field(default_factory=list)


class _BoundedHTMLTreeParser(HTMLParser):
    """A tiny, bounded DOM sufficient for the publisher listing pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HTMLNode("document")
        self._stack = [self.root]
        self._node_count = 0

    def _add_node(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> _HTMLNode:
        self._node_count += 1
        if self._node_count > _HTML_MAX_NODES:
            raise ValueError("HTML document contains too many elements")
        if len(self._stack) > _HTML_MAX_DEPTH:
            raise ValueError("HTML document is nested too deeply")

        normalized_attrs = {
            key.lower(): value or ""
            for key, value in attrs
            if key
        }
        node = _HTMLNode(tag.lower(), normalized_attrs, self._stack[-1])
        self._stack[-1].content.append(node)
        return node

    def handle_starttag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        node = self._add_node(tag, attrs)
        if node.tag not in _VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        self._add_node(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == lowered:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        current = self._stack[-1]
        is_json_ld = (
            current.tag == "script"
            and current.attrs.get("type", "").lower().split(";", 1)[0].strip()
            == "application/ld+json"
        )
        if data and (current.tag not in _TEXT_IGNORED_ELEMENTS or is_json_ld):
            self._stack[-1].content.append(data)


@dataclass
class _HTMLRecord:
    url: str
    title: str = ""
    summary: str = ""
    author: str = ""
    category: str = ""
    published: str = ""
    modified: str = ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _direct_elements(element: ET.Element, names: Iterable[str]) -> List[ET.Element]:
    wanted = {name.lower() for name in names}
    return [child for child in list(element) if _local_name(child.tag) in wanted]


def _element_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return clean_text(" ".join(element.itertext()))


def _raw_element_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", " ".join(element.itertext())).strip()


def _first_element_text(element: ET.Element, names: Iterable[str]) -> str:
    for child in _direct_elements(element, names):
        value = _element_text(child)
        if value:
            return value
    return ""


def _xml_link(entry: ET.Element) -> str:
    links = _direct_elements(entry, ("link",))
    alternate = ""
    fallback = ""
    for link in links:
        # ``clean_text`` intentionally parses HTML and can buffer a URL ending
        # in an ampersand query parameter as a partial character reference.
        value = (link.attrib.get("href") or _raw_element_text(link)).strip()
        if not value:
            continue
        if not fallback:
            fallback = value
        rel = clean_text(link.attrib.get("rel")).lower()
        if rel in ("", "alternate"):
            alternate = value
            break
    return alternate or fallback


def _xml_author(entry: ET.Element) -> str:
    for author in _direct_elements(entry, ("author", "creator")):
        name = _first_element_text(author, ("name",))
        value = name or _element_text(author)
        if value:
            return value
    return ""


def _xml_category(entry: ET.Element) -> str:
    for category in _direct_elements(entry, ("category", "subject")):
        value = clean_text(
            category.attrib.get("term")
            or category.attrib.get("label")
            or _element_text(category)
        )
        if value:
            return value
    return ""


def _same_publisher_host(url: str, source: SourceConfig) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    expected_hosts = {
        (urlsplit(source.home_url).hostname or "").lower(),
        (urlsplit(source.fetch_url).hostname or "").lower(),
    }
    expected_hosts.discard("")
    for expected in expected_hosts:
        if hostname == expected:
            return True
        if hostname.removeprefix("www.") == expected.removeprefix("www."):
            return True
    return False


def _canonical_article_url(raw_url: str, source: SourceConfig) -> Optional[str]:
    try:
        url = canonicalize_url(raw_url, source.home_url)
    except (TypeError, ValueError):
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not _same_publisher_host(url, source):
        return None
    return url


def _fixed_path_url(
    raw_url: str,
    source: SourceConfig,
    section: str,
    blocked_slugs: Set[str],
) -> Optional[str]:
    url = _canonical_article_url(raw_url, source)
    if not url:
        return None
    decoded_path = unquote(urlsplit(url).path).strip("/")
    segments = decoded_path.split("/") if decoded_path else []
    if len(segments) != 2 or segments[0].lower() != section:
        return None
    slug = segments[1].strip().lower()
    if not slug or slug in blocked_slugs or slug.startswith("."):
        return None
    return url


def _reference_time(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clean_field(value: str, limit: Optional[int] = None) -> str:
    """Clean inline markup without leaving spaces before punctuation."""

    text = clean_text(value)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if limit and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _make_candidate(
    source: SourceConfig,
    record: _HTMLRecord,
    fetched_at: Optional[datetime],
    external_id: str = "",
) -> Optional[ArticleCandidate]:
    title = _clean_field(record.title, limit=500)
    url = _canonical_article_url(record.url, source)
    if not title or not url:
        return None
    summary = _clean_field(record.summary, limit=_SUMMARY_LIMIT)
    author = _clean_field(record.author, limit=200)
    category = _clean_field(record.category, limit=120)
    published_at = parse_datetime(record.published, now=_reference_time(fetched_at))
    modified_at = parse_datetime(record.modified, now=_reference_time(fetched_at))
    identifier = re.sub(r"\s+", " ", external_id or "").strip()[:1000] or url
    content_hash = stable_hash(
        url,
        title,
        summary,
        author,
        category,
        published_at,
        modified_at,
    )
    return ArticleCandidate(
        source_slug=source.slug,
        external_id=identifier,
        url=url,
        title=title,
        summary=summary,
        author=author,
        category=category,
        published_at=published_at,
        modified_at=modified_at,
        content_hash=content_hash,
    )


def _merge_text(left: str, right: str, prefer_longer: bool = False) -> str:
    if not left:
        return right
    if not right:
        return left
    if prefer_longer and len(right) > len(left):
        return right
    return left


def _merge_candidates(left: ArticleCandidate, right: ArticleCandidate) -> ArticleCandidate:
    title = _merge_text(left.title, right.title, prefer_longer=True)
    summary = _merge_text(left.summary, right.summary, prefer_longer=True)
    author = _merge_text(left.author, right.author)
    category = _merge_text(left.category, right.category)
    published_at = left.published_at or right.published_at
    modified_at = left.modified_at or right.modified_at
    external_id = left.external_id or right.external_id or left.url
    return ArticleCandidate(
        source_slug=left.source_slug,
        external_id=external_id,
        url=left.url,
        title=title,
        summary=summary,
        author=author,
        category=category,
        published_at=published_at,
        modified_at=modified_at,
        content_hash=stable_hash(
            left.url,
            title,
            summary,
            author,
            category,
            published_at,
            modified_at,
        ),
    )


def _deduplicate(candidates: Iterable[ArticleCandidate]) -> List[ArticleCandidate]:
    ordered_urls: List[str] = []
    by_url: Dict[str, ArticleCandidate] = {}
    for candidate in candidates:
        existing = by_url.get(candidate.url)
        if existing is None:
            ordered_urls.append(candidate.url)
            by_url[candidate.url] = candidate
        else:
            by_url[candidate.url] = _merge_candidates(existing, candidate)
    return [by_url[url] for url in ordered_urls]


def _parse_feed(
    source: SourceConfig, body: bytes, fetched_at: Optional[datetime]
) -> List[ArticleCandidate]:
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("RSS/Atom document types and entities are not allowed")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError("invalid RSS/Atom XML: %s" % exc) from exc

    root_name = _local_name(root.tag)
    if root_name not in ("rss", "rdf", "feed", "channel"):
        raise ValueError("unsupported feed root element: %s" % root_name)

    entries = [
        element
        for element in root.iter()
        if _local_name(element.tag) in ("item", "entry")
    ]
    candidates: List[ArticleCandidate] = []
    for entry in entries:
        raw_url = _xml_link(entry)
        identifier = _first_element_text(entry, ("guid", "id"))
        if not raw_url and identifier.lower().startswith(("http://", "https://")):
            raw_url = identifier
        url = _canonical_article_url(raw_url, source) if raw_url else None
        if not url:
            continue

        summary = _first_element_text(entry, ("description", "summary", "encoded", "content"))
        published = _first_element_text(entry, ("pubdate", "published", "date"))
        modified = _first_element_text(entry, ("updated", "modified"))
        if not published:
            published = modified
        record = _HTMLRecord(
            url=url,
            title=_first_element_text(entry, ("title",)),
            summary=summary,
            author=_xml_author(entry),
            category=_xml_category(entry),
            published=published,
            modified=modified,
        )
        candidate = _make_candidate(source, record, fetched_at, external_id=identifier)
        if candidate:
            candidates.append(candidate)
    return _deduplicate(candidates)


def _classes(node: _HTMLNode) -> Set[str]:
    return {part.lower() for part in node.attrs.get("class", "").split() if part}


def _class_contains(node: _HTMLNode, value: str) -> bool:
    return value.lower() in node.attrs.get("class", "").lower()


def _descendants(node: _HTMLNode, include_self: bool = False) -> Iterable[_HTMLNode]:
    stack: List[_HTMLNode] = [node] if include_self else [
        item for item in reversed(node.content) if isinstance(item, _HTMLNode)
    ]
    while stack:
        current = stack.pop()
        yield current
        children = [item for item in current.content if isinstance(item, _HTMLNode)]
        stack.extend(reversed(children))


def _node_text(node: Optional[_HTMLNode], limit: Optional[int] = None) -> str:
    if node is None:
        return ""
    parts: List[str] = []
    stack: List[Union[str, _HTMLNode]] = list(reversed(node.content))
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        if item.tag in _TEXT_IGNORED_ELEMENTS:
            continue
        stack.extend(reversed(item.content))
    return clean_text(" ".join(parts), limit=limit)


def _first_descendant(
    node: _HTMLNode, predicate
) -> Optional[_HTMLNode]:
    for descendant in _descendants(node):
        if predicate(descendant):
            return descendant
    return None


def _first_nonempty_text(nodes: Iterable[Optional[_HTMLNode]], limit: Optional[int] = None) -> str:
    for node in nodes:
        value = _node_text(node, limit=limit)
        if value:
            return value
    return ""


def _date_text(node: _HTMLNode) -> str:
    if node.tag == "time":
        value = clean_text(node.attrs.get("datetime")) or _node_text(node)
        if value:
            return value
    for descendant in _descendants(node):
        if descendant.tag == "time":
            value = clean_text(descendant.attrs.get("datetime")) or _node_text(descendant)
            if value:
                return value
    for descendant in _descendants(node, include_self=True):
        value = _node_text(descendant)
        if value and len(value) <= 40 and _DATE_PATTERN.match(value):
            return value
    return ""


def _parse_html_tree(body: bytes) -> _HTMLNode:
    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("HTML response is not valid UTF-8") from exc
    parser = _BoundedHTMLTreeParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, RecursionError) as exc:
        raise ValueError("unsafe or invalid HTML: %s" % exc) from exc
    return parser.root


def _openai_records(root: _HTMLNode, source: SourceConfig) -> List[_HTMLRecord]:
    blocked = {"topic", "topics", "category", "categories", "tag", "author", "page"}
    records: List[_HTMLRecord] = []
    for anchor in _descendants(root):
        if anchor.tag != "a" or "resource-item" not in _classes(anchor):
            continue
        url = _fixed_path_url(anchor.attrs.get("href", ""), source, "blog", blocked)
        if not url:
            continue
        image = _first_descendant(anchor, lambda item: item.tag == "img" and bool(item.attrs.get("alt")))
        title_node = _first_descendant(anchor, lambda item: _class_contains(item, "line-clamp-2"))
        heading = _first_descendant(anchor, lambda item: item.tag in ("h1", "h2", "h3", "h4"))
        title = clean_text(image.attrs.get("alt"), limit=500) if image else ""
        title = title or _first_nonempty_text((title_node, heading), limit=500)
        summary_node = _first_descendant(
            anchor,
            lambda item: item.tag == "p" and _class_contains(item, "line-clamp-3"),
        ) or _first_descendant(anchor, lambda item: item.tag == "p")
        date_node = _first_descendant(
            anchor,
            lambda item: "pt-4" in _classes(item) and "text-secondary" in _classes(item),
        )
        category_node = _first_descendant(
            anchor,
            lambda item: (
                "pt-2" in _classes(item)
                and "text-sm" in _classes(item)
                and "text-secondary" in _classes(item)
            ),
        )
        records.append(
            _HTMLRecord(
                url=url,
                title=title,
                summary=_node_text(summary_node, limit=_SUMMARY_LIMIT),
                category=_node_text(category_node, limit=120),
                published=_node_text(date_node, limit=40) or _date_text(anchor),
            )
        )
    return records


def _openai_markdown_records(body: bytes, source: SourceConfig) -> List[_HTMLRecord]:
    """Parse the compact official ``/blog.md`` index.

    Its Posts section is intentionally regular: ``- [title](post.md): summary``.
    Keeping this parser line-oriented avoids downloading and walking the much
    larger visual listing page.
    """

    try:
        document = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("OpenAI blog index is not valid UTF-8") from exc
    item_pattern = re.compile(
        r"^\s*-\s+\[([^\]]+)\]\(([^)\s]+)\)(?::\s*(.*?))?\s*$"
    )
    blocked = {"topic", "topics", "category", "categories", "tag", "author", "page"}
    records: List[_HTMLRecord] = []
    for line in document.splitlines():
        match = item_pattern.match(line)
        if not match:
            continue
        raw_title, raw_url, raw_summary = match.groups()
        candidate = _canonical_article_url(raw_url, source)
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if not parts.path.lower().endswith(".md"):
            continue
        article_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path[:-3], parts.query, "")
        )
        article_url = _fixed_path_url(article_url, source, "blog", blocked)
        if not article_url:
            continue
        records.append(
            _HTMLRecord(
                url=article_url,
                title=raw_title,
                summary=raw_summary or "",
            )
        )
    return records


def _claude_container(node: _HTMLNode) -> bool:
    if node.attrs.get("role", "").lower() != "listitem":
        return False
    classes = _classes(node)
    return "blog_cms_item" in classes or "marquee_cms_blog_list_item" in classes


def _claude_records(root: _HTMLNode, source: SourceConfig) -> List[_HTMLRecord]:
    blocked = {"topic", "topics", "category", "categories", "tag", "author", "page"}
    records: List[_HTMLRecord] = []
    for container in _descendants(root):
        if not _claude_container(container):
            continue
        anchors = [
            item
            for item in _descendants(container)
            if item.tag == "a" and item.attrs.get("href")
        ]
        url = ""
        link_title = ""
        for anchor in anchors:
            candidate_url = _fixed_path_url(anchor.attrs["href"], source, "blog", blocked)
            if candidate_url:
                url = candidate_url
                link_title = clean_text(anchor.attrs.get("data-cta-copy"), limit=500)
                break
        if not url:
            continue

        heading_field = _first_descendant(
            container, lambda item: item.attrs.get("fs-list-field", "").lower() == "heading"
        )
        title_node = _first_descendant(container, lambda item: _class_contains(item, "card_blog_title"))
        heading_node = _first_descendant(
            container, lambda item: item.tag in ("h1", "h2", "h3", "h4")
        )
        date_field = _first_descendant(
            container,
            lambda item: item.attrs.get("fs-list-field", "").lower() == "date",
        )
        category_field = _first_descendant(
            container,
            lambda item: item.attrs.get("fs-list-field", "").lower() == "category",
        )
        summary_field = _first_descendant(
            container,
            lambda item: item.attrs.get("fs-list-field", "").lower()
            in ("summary", "description", "excerpt"),
        )
        summary_node = summary_field or _first_descendant(
            container,
            lambda item: item.tag == "p" and (
                _class_contains(item, "summary")
                or _class_contains(item, "description")
                or _class_contains(item, "excerpt")
            ),
        )
        records.append(
            _HTMLRecord(
                url=url,
                title=_first_nonempty_text((heading_field, title_node, heading_node), limit=500)
                or link_title,
                summary=_node_text(summary_node, limit=_SUMMARY_LIMIT),
                category=_node_text(category_field, limit=120),
                published=_node_text(date_field, limit=40) or _date_text(container),
            )
        )
    return records


def _anthropic_anchor(anchor: _HTMLNode) -> bool:
    if anchor.tag != "a":
        return False
    class_value = anchor.attrs.get("class", "").lower()
    return "publicationlist" in class_value or "featuredgrid" in class_value


def _anthropic_records(root: _HTMLNode, source: SourceConfig) -> List[_HTMLRecord]:
    blocked = {"topic", "topics", "category", "categories", "tag", "author", "page"}
    records: List[_HTMLRecord] = []
    for anchor in _descendants(root):
        if not _anthropic_anchor(anchor):
            continue
        url = _fixed_path_url(anchor.attrs.get("href", ""), source, "news", blocked)
        if not url:
            continue
        title_node = _first_descendant(
            anchor,
            lambda item: _class_contains(item, "title")
            and not _class_contains(item, "subtitle"),
        ) or _first_descendant(
            anchor, lambda item: item.tag in ("h1", "h2", "h3", "h4", "h5", "h6")
        )
        summary_node = _first_descendant(anchor, lambda item: item.tag == "p")
        subject_node = _first_descendant(anchor, lambda item: _class_contains(item, "subject"))
        if subject_node is None:
            meta_node = _first_descendant(anchor, lambda item: _class_contains(item, "meta"))
            if meta_node is not None:
                subject_node = _first_descendant(
                    meta_node,
                    lambda item: item.tag == "span" and not _class_contains(item, "title"),
                )
        records.append(
            _HTMLRecord(
                url=url,
                title=_node_text(title_node, limit=500),
                summary=_node_text(summary_node, limit=_SUMMARY_LIMIT),
                category=_node_text(subject_node, limit=120),
                published=_date_text(anchor),
            )
        )
    return records


def _parse_fixed_html(
    source: SourceConfig, body: bytes, fetched_at: Optional[datetime]
) -> List[ArticleCandidate]:
    if source.adapter == "openai_developers":
        markdown_records = _openai_markdown_records(body, source)
        if markdown_records:
            candidates = [
                candidate
                for candidate in (
                    _make_candidate(source, record, fetched_at)
                    for record in markdown_records
                )
                if candidate is not None
            ]
            return _deduplicate(candidates)

    root = _parse_html_tree(body)
    if source.adapter == "openai_developers":
        records = _openai_records(root, source)
    elif source.adapter == "claude_blog":
        records = _claude_records(root, source)
    elif source.adapter == "anthropic_news":
        records = _anthropic_records(root, source)
    else:
        raise ValueError("unsupported HTML adapter: %s" % source.adapter)
    candidates = [
        candidate
        for candidate in (
            _make_candidate(source, record, fetched_at)
            for record in records
        )
        if candidate is not None
    ]
    return _deduplicate(candidates)


def _safe_xml_root(body: bytes, document_name: str) -> ET.Element:
    if not isinstance(body, bytes):
        raise TypeError("%s body must be bytes" % document_name)
    if not body.strip():
        raise ValueError("%s body is empty" % document_name)
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("%s document types and entities are not allowed" % document_name)
    try:
        return ET.fromstring(body)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError("invalid %s XML: %s" % (document_name, exc)) from exc


def _url_has_prefix(url: str, prefix: str) -> bool:
    parts = urlsplit(url)
    prefix_parts = urlsplit(prefix)
    if parts.scheme not in ("http", "https"):
        return False
    if (parts.hostname or "").lower().removeprefix("www.") != (
        prefix_parts.hostname or ""
    ).lower().removeprefix("www."):
        return False
    candidate_path = unquote(parts.path or "/").rstrip("/") or "/"
    prefix_path = unquote(prefix_parts.path or "/").rstrip("/") or "/"
    if prefix_path == "/":
        return True
    return candidate_path == prefix_path or candidate_path.startswith(prefix_path + "/")


def parse_sitemap(body: bytes, url_prefix: str) -> List[Tuple[str, Optional[str]]]:
    """Parse a standard sitemap URL set or sitemap index.

    Only same-site locations below ``url_prefix`` are returned.  This makes the
    helper usable both for a site-root sitemap index and for filtering an URL set
    down to a section such as ``/news``.
    """

    if not isinstance(url_prefix, str) or not url_prefix.strip():
        raise ValueError("sitemap URL prefix is empty")
    try:
        prefix = canonicalize_url(url_prefix)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid sitemap URL prefix: %s" % exc) from exc
    root = _safe_xml_root(body, "sitemap")
    root_name = _local_name(root.tag)
    if root_name == "urlset":
        entry_name = "url"
    elif root_name == "sitemapindex":
        entry_name = "sitemap"
    else:
        raise ValueError("unsupported sitemap root element: %s" % root_name)

    ordered: List[str] = []
    entries: Dict[str, Optional[str]] = {}
    base_url = url_prefix if url_prefix.rstrip().endswith("/") else url_prefix.rstrip() + "/"
    for entry in root.iter():
        if _local_name(entry.tag) != entry_name:
            continue
        loc_nodes = _direct_elements(entry, ("loc",))
        if not loc_nodes:
            continue
        raw_location = _raw_element_text(loc_nodes[0])
        try:
            location = canonicalize_url(raw_location, base_url)
        except (TypeError, ValueError):
            continue
        if not _url_has_prefix(location, prefix):
            continue
        raw_lastmod = _first_element_text(entry, ("lastmod",))
        lastmod = parse_datetime(raw_lastmod)
        if location not in entries:
            ordered.append(location)
            entries[location] = lastmod
        elif entries[location] is None and lastmod is not None:
            entries[location] = lastmod
    return [(location, entries[location]) for location in ordered]


def _meta_content(root: _HTMLNode, labels: Sequence[str]) -> str:
    wanted = {label.lower() for label in labels}
    for node in _descendants(root):
        if node.tag != "meta":
            continue
        label = (
            node.attrs.get("property")
            or node.attrs.get("name")
            or node.attrs.get("itemprop")
            or ""
        ).lower()
        if label in wanted:
            value = clean_text(node.attrs.get("content"))
            if value:
                return value
    return ""


def _canonical_page_url(root: _HTMLNode, source: SourceConfig, fetched_url: str) -> str:
    raw_urls: List[str] = []
    for node in _descendants(root):
        if node.tag == "link" and "canonical" in {
            value.lower() for value in node.attrs.get("rel", "").split()
        }:
            raw_urls.append(node.attrs.get("href", ""))
            break
    og_url = _meta_content(root, ("og:url",))
    if og_url:
        raw_urls.append(og_url)
    raw_urls.append(fetched_url)
    for raw_url in raw_urls:
        url = _canonical_article_url(raw_url, source)
        if url:
            return url
    raise ValueError("article page has no safe canonical URL")


def _json_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            scalar = _json_scalar(item)
            if scalar:
                return scalar
        return ""
    if isinstance(value, dict):
        for key in ("@value", "name", "@id", "url"):
            scalar = _json_scalar(value.get(key))
            if scalar:
                return scalar
    return ""


def _json_author(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    names: List[str] = []
    for item in values:
        if isinstance(item, dict):
            name = _json_scalar(item.get("name"))
        else:
            name = _json_scalar(item)
        name = clean_text(name, limit=200)
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def _json_objects(value: Any) -> Iterable[Dict[str, Any]]:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def _json_type_names(value: Any) -> Set[str]:
    raw_values = value if isinstance(value, list) else [value]
    names: Set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        names.add(raw.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower())
    return names


def _json_ld_article(root: _HTMLNode) -> Optional[Dict[str, Any]]:
    by_priority: Dict[str, List[Dict[str, Any]]] = {
        "blogposting": [],
        "newsarticle": [],
        "article": [],
    }
    for script in _descendants(root):
        if script.tag != "script":
            continue
        if script.attrs.get("type", "").lower().split(";", 1)[0].strip() != "application/ld+json":
            continue
        raw_json = "".join(
            item for item in script.content if isinstance(item, str)
        ).strip()
        if raw_json.startswith("<!--") and raw_json.endswith("-->"):
            raw_json = raw_json[4:-3].strip()
        try:
            payload = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for item in _json_objects(payload):
            types = _json_type_names(item.get("@type"))
            for article_type in by_priority:
                if article_type in types:
                    by_priority[article_type].append(item)
                    break
    for article_type in ("blogposting", "newsarticle", "article"):
        if by_priority[article_type]:
            return by_priority[article_type][0]
    return None


def _page_date(
    root: _HTMLNode,
    meta_labels: Sequence[str],
    fetched_at: Optional[datetime],
) -> str:
    meta_value = _meta_content(root, meta_labels)
    if meta_value:
        return meta_value
    for node in _descendants(root):
        if node.tag == "time":
            value = clean_text(node.attrs.get("datetime")) or _node_text(node, limit=80)
            if value:
                return value
    for node in _descendants(root):
        if not (_class_contains(node, "date") or _class_contains(node, "publish")):
            continue
        value = _node_text(node, limit=80)
        if value and parse_datetime(value, now=_reference_time(fetched_at)):
            return value
    # Last-resort support for a plain date text node, without repeatedly walking
    # every descendant subtree.
    for node in _descendants(root):
        if any(isinstance(item, _HTMLNode) for item in node.content):
            continue
        value = clean_text(" ".join(item for item in node.content if isinstance(item, str)))
        if value and len(value) <= 40 and _DATE_PATTERN.match(value):
            return value
    return ""


def _fallback_page_record(
    root: _HTMLNode,
    source: SourceConfig,
    fetched_url: str,
    fetched_at: Optional[datetime],
) -> _HTMLRecord:
    title = _meta_content(root, ("og:title", "twitter:title"))
    if not title:
        heading = _first_descendant(root, lambda node: node.tag == "h1")
        title_node = _first_descendant(root, lambda node: node.tag == "title")
        title = _first_nonempty_text((heading, title_node), limit=500)
    summary = _meta_content(root, ("description", "og:description", "twitter:description"))
    author = _meta_content(root, ("author", "article:author"))
    category = _meta_content(root, ("article:section", "section"))
    published = _page_date(
        root,
        ("article:published_time", "datepublished", "publish-date", "pubdate"),
        fetched_at,
    )
    modified = _meta_content(root, ("article:modified_time", "datemodified", "last-modified"))
    return _HTMLRecord(
        url=_canonical_page_url(root, source, fetched_url),
        title=title,
        summary=summary,
        author=author,
        category=category,
        published=published,
        modified=modified,
    )


def parse_article_page(
    source: SourceConfig,
    body: bytes,
    url: str,
    fetched_at: Optional[datetime] = None,
) -> ArticleCandidate:
    """Parse deterministic metadata from an individual article page.

    Claude's JSON-LD ``BlogPosting`` metadata is preferred when present.  The
    normal canonical/Open Graph/meta/H1 fields remain a publisher-independent
    fallback for OpenAI and Anthropic pages and for temporary JSON-LD drift.
    """

    if not isinstance(body, bytes):
        raise TypeError("article page body must be bytes")
    if not body.strip():
        raise ValueError("article page body is empty")
    if fetched_at is not None and not isinstance(fetched_at, datetime):
        raise TypeError("fetched_at must be a datetime or None")
    fetched_url = _canonical_article_url(url, source)
    if not fetched_url:
        raise ValueError("article page URL is outside the configured publisher")

    root = _parse_html_tree(body)
    fallback = _fallback_page_record(root, source, fetched_url, fetched_at)
    json_article = _json_ld_article(root)
    if json_article is not None:
        raw_json_url = _json_scalar(json_article.get("url")) or _json_scalar(
            json_article.get("mainEntityOfPage")
        )
        json_url = _canonical_article_url(raw_json_url, source) if raw_json_url else None
        record = _HTMLRecord(
            url=json_url or fallback.url,
            title=_json_scalar(json_article.get("headline"))
            or _json_scalar(json_article.get("name"))
            or fallback.title,
            summary=_json_scalar(json_article.get("description")) or fallback.summary,
            author=_json_author(json_article.get("author")) or fallback.author,
            category=_json_scalar(json_article.get("articleSection")) or fallback.category,
            published=_json_scalar(json_article.get("datePublished")) or fallback.published,
            modified=_json_scalar(json_article.get("dateModified")) or fallback.modified,
        )
    else:
        record = fallback
    candidate = _make_candidate(source, record, fetched_at)
    if candidate is None:
        raise ValueError("article page contains no usable title and canonical URL")
    return candidate


def parse_source(
    source: SourceConfig,
    body: bytes,
    fetched_at: Optional[datetime] = None,
) -> List[ArticleCandidate]:
    """Parse one fetched source response into normalized, de-duplicated articles.

    A parser returning no usable articles is treated as an error.  This makes a
    publisher redesign visible to the scheduler instead of silently recording a
    successful sync with zero discoveries.
    """

    if not isinstance(body, bytes):
        raise TypeError("source body must be bytes")
    if not body.strip():
        raise ValueError("source body is empty")
    if fetched_at is not None and not isinstance(fetched_at, datetime):
        raise TypeError("fetched_at must be a datetime or None")

    if source.adapter == "rss":
        candidates = _parse_feed(source, body, fetched_at)
    elif source.adapter in ("openai_developers", "claude_blog", "anthropic_news"):
        candidates = _parse_fixed_html(source, body, fetched_at)
    else:
        raise ValueError("unsupported source adapter: %s" % source.adapter)

    if not candidates:
        raise ValueError("%s parser found no articles" % source.adapter)
    return candidates


__all__ = ["parse_article_page", "parse_sitemap", "parse_source"]
