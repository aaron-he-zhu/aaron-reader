import sys
from datetime import datetime, timezone
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.models import SourceConfig  # noqa: E402
from aaron_reader.parsers import (  # noqa: E402
    parse_article_page,
    parse_sitemap,
    parse_source,
)


FIXTURES = Path(__file__).with_name("fixtures")
FETCHED_AT = datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source(adapter: str) -> SourceConfig:
    values = {
        "rss": (
            "openai-news",
            "OpenAI News",
            "https://openai.com/news/",
            "https://openai.com/news/rss.xml",
        ),
        "openai_developers": (
            "openai-developers",
            "OpenAI Developer Blog",
            "https://developers.openai.com/blog",
            "https://developers.openai.com/blog",
        ),
        "claude_blog": (
            "claude-blog",
            "Claude Blog",
            "https://claude.com/blog/",
            "https://claude.com/blog/",
        ),
        "anthropic_news": (
            "anthropic-news",
            "Anthropic News",
            "https://www.anthropic.com/news",
            "https://www.anthropic.com/news",
        ),
        "cursor_blog": (
            "cursor-blog",
            "Cursor Blog",
            "https://cursor.com/blog",
            "https://cursor.com/blog",
        ),
    }
    slug, name, home_url, fetch_url = values[adapter]
    return SourceConfig(
        slug=slug,
        name=name,
        home_url=home_url,
        fetch_url=fetch_url,
        adapter=adapter,
    )


class FeedParserTests(unittest.TestCase):
    def test_rss_fields_are_normalized(self) -> None:
        articles = parse_source(source("rss"), fixture("rss.xml"), FETCHED_AT)

        self.assertEqual(2, len(articles))
        first = articles[0]
        self.assertEqual("openai-news", first.source_slug)
        self.assertEqual("openai-post-001", first.external_id)
        self.assertEqual("https://openai.com/index/building-safely", first.url)
        self.assertEqual("Building & shipping safely", first.title)
        self.assertEqual("A deterministic release update.", first.summary)
        self.assertEqual("OpenAI Safety Team", first.author)
        self.assertEqual("Safety", first.category)
        self.assertEqual("2026-07-30T09:15:00Z", first.published_at)
        self.assertIsNone(first.modified_at)
        self.assertEqual(64, len(first.content_hash))

        second = articles[1]
        self.assertEqual("Tools that help teams move faster.", second.summary)
        self.assertEqual("Company", second.category)
        self.assertEqual("2026-07-28T16:00:00Z", second.published_at)

    def test_atom_links_author_category_and_dates(self) -> None:
        articles = parse_source(source("rss"), fixture("atom.xml"), FETCHED_AT)

        self.assertEqual(1, len(articles))
        article = articles[0]
        self.assertEqual("tag:openai.com,2026:atom-1", article.external_id)
        self.assertEqual("https://openai.com/index/atom-entry", article.url)
        self.assertEqual("Atom support", article.title)
        self.assertEqual("A concise Atom summary.", article.summary)
        self.assertEqual("Ada Example", article.author)
        self.assertEqual("Research", article.category)
        self.assertEqual("2026-07-25T00:30:00Z", article.published_at)
        self.assertEqual("2026-07-26T01:00:00Z", article.modified_at)

    def test_doctype_and_entity_declarations_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "entities are not allowed"):
            parse_source(source("rss"), fixture("malicious_rss.xml"), FETCHED_AT)


class HTMLParserTests(unittest.TestCase):
    def test_openai_markdown_index(self) -> None:
        articles = parse_source(
            source("openai_developers"), fixture("openai_blog.md"), FETCHED_AT
        )

        self.assertEqual(2, len(articles))
        first = articles[0]
        self.assertEqual(
            "https://developers.openai.com/blog/15-lessons-building-chatgpt-apps",
            first.url,
        )
        self.assertEqual("15 lessons learned building ChatGPT Apps", first.title)
        self.assertEqual("Build ChatGPT Apps 10x faster.", first.summary)
        second = articles[1]
        self.assertEqual("Run long horizon tasks with Codex", second.title)
        self.assertEqual("", second.summary)

    def test_openai_developer_cards_and_filtering(self) -> None:
        articles = parse_source(
            source("openai_developers"),
            fixture("openai_developers.html"),
            FETCHED_AT,
        )

        self.assertEqual(2, len(articles))
        first = articles[0]
        self.assertEqual(
            "https://developers.openai.com/blog/custom-code-review-rules-for-codex",
            first.url,
        )
        self.assertEqual("Custom Code Review rules for Codex", first.title)
        self.assertEqual("Provide detailed rules for safer reviews.", first.summary)
        self.assertEqual("Codex", first.category)
        self.assertEqual("2026-07-20T00:00:00Z", first.published_at)

        second = articles[1]
        self.assertEqual("Voice search & the Realtime API", second.title)
        self.assertEqual("Audio", second.category)
        # A no-year date that lies well ahead of fetched_at belongs to last year.
        self.assertEqual("2025-12-30T00:00:00Z", second.published_at)
        self.assertNotIn("topic", {item.url for item in articles})

    def test_claude_cards_are_enriched_and_deduplicated(self) -> None:
        articles = parse_source(
            source("claude_blog"), fixture("claude_blog.html"), FETCHED_AT
        )

        self.assertEqual(2, len(articles))
        first = articles[0]
        self.assertEqual("https://claude.com/blog/bringing-mcp-to-claude", first.url)
        self.assertEqual("Bringing MCP to Claude", first.title)
        self.assertEqual("A practical guide to the latest MCP support.", first.summary)
        self.assertEqual("Product announcements", first.category)
        self.assertEqual("2026-07-28T00:00:00Z", first.published_at)

        second = articles[1]
        self.assertEqual("Build reliable agents", second.title)
        self.assertEqual("Engineering", second.category)
        self.assertEqual("2026-07-24T00:00:00Z", second.published_at)

    def test_anthropic_featured_and_publication_rows_are_deduplicated(self) -> None:
        articles = parse_source(
            source("anthropic_news"), fixture("anthropic_news.html"), FETCHED_AT
        )

        self.assertEqual(3, len(articles))
        by_url = {article.url: article for article in articles}
        opus = by_url["https://www.anthropic.com/news/claude-opus-5"]
        self.assertEqual("Introducing Claude Opus 5", opus.title)
        self.assertEqual(
            "Our most capable model for coding and professional work.", opus.summary
        )
        self.assertEqual("Product", opus.category)
        self.assertEqual("2026-07-24T00:00:00Z", opus.published_at)

        incident = by_url["https://www.anthropic.com/news/investigating-incidents"]
        self.assertEqual("Investigating real-world incidents", incident.title)
        self.assertEqual("Frontier Red Team", incident.category)
        self.assertEqual("2026-07-30T00:00:00Z", incident.published_at)
        self.assertNotIn("https://www.anthropic.com/news/unrelated-footer-link", by_url)

    def test_cursor_blog_featured_and_directory_items(self) -> None:
        articles = parse_source(
            source("cursor_blog"), fixture("cursor_blog.html"), FETCHED_AT
        )

        self.assertEqual(4, len(articles))
        by_url = {article.url: article for article in articles}

        git_article = by_url["https://cursor.com/blog/git-at-any-scale"]
        self.assertEqual("Git at any scale", git_article.title)
        self.assertEqual(
            "How Cursor handles monorepos with millions of files.",
            git_article.summary,
        )
        self.assertEqual("Research", git_article.category)
        self.assertEqual("2026-08-18T12:00:00Z", git_article.published_at)

        grok = by_url["https://cursor.com/blog/grok-4-6"]
        self.assertEqual("Introducing Grok 4.6", grok.title)
        self.assertEqual("Research", grok.category)
        self.assertEqual("2026-08-12T00:00:00Z", grok.published_at)

        builds = by_url["https://cursor.com/blog/builds"]
        self.assertEqual("Cloud agents start 3x faster with builds", builds.title)
        self.assertEqual("Product", builds.category)
        self.assertEqual("2026-08-13T12:00:00Z", builds.published_at)

        spacex = by_url["https://cursor.com/blog/joining-spacex"]
        self.assertEqual("Cursor is now a part of SpaceX", spacex.title)
        self.assertEqual("Company", spacex.category)

        self.assertNotIn("https://cursor.com/blog/topic/research", by_url)

    def test_cursor_blog_topic_urls_are_filtered(self) -> None:
        html = b"""
            <html><body>
              <a class="card card--media card--feature" href="/blog/topic/research">
                <p class="type-md text-theme-text">Topic page</p>
              </a>
              <a class="blog-directory__row" href="/blog/category">
                <p class="type-base text-theme-text text-pretty">Category page</p>
              </a>
            </body></html>
        """
        with self.assertRaisesRegex(ValueError, "found no articles"):
            parse_source(source("cursor_blog"), html, FETCHED_AT)

    def test_no_matching_articles_is_an_error(self) -> None:
        for adapter in ("openai_developers", "claude_blog", "anthropic_news", "cursor_blog"):
            with self.subTest(adapter=adapter):
                with self.assertRaisesRegex(ValueError, "found no articles"):
                    parse_source(source(adapter), fixture("empty.html"), FETCHED_AT)

    def test_path_and_markup_attacks_do_not_become_articles(self) -> None:
        malicious = b"""
            <html><body>
              <a class="resource-item" href="javascript:alert(1)">
                <img alt="Unsafe" />
              </a>
              <a class="resource-item" href="/blog/topic%2Fhidden">
                <img alt="Encoded topic" />
              </a>
              <script><a class="resource-item" href="/blog/script-injection">Bad</a></script>
            </body></html>
        """
        with self.assertRaisesRegex(ValueError, "found no articles"):
            parse_source(source("openai_developers"), malicious, FETCHED_AT)

    def test_excessively_nested_html_is_rejected(self) -> None:
        malicious = (b"<div>" * 300) + (b"</div>" * 300)
        with self.assertRaisesRegex(ValueError, "nested too deeply"):
            parse_source(source("claude_blog"), malicious, FETCHED_AT)


class PublicContractTests(unittest.TestCase):
    def test_unknown_adapter_and_empty_body_are_errors(self) -> None:
        unknown = SourceConfig(
            slug="unknown",
            name="Unknown",
            home_url="https://example.com",
            fetch_url="https://example.com",
            adapter="magic",
        )
        with self.assertRaisesRegex(ValueError, "unsupported source adapter"):
            parse_source(unknown, b"content", FETCHED_AT)
        with self.assertRaisesRegex(ValueError, "body is empty"):
            parse_source(source("rss"), b"  \n", FETCHED_AT)


class SitemapParserTests(unittest.TestCase):
    def test_urlset_is_normalized_filtered_and_deduplicated(self) -> None:
        entries = parse_sitemap(
            fixture("sitemap.xml"), "https://www.anthropic.com/news/"
        )

        self.assertEqual(
            [
                (
                    "https://www.anthropic.com/news/first-story",
                    "2026-07-30T00:00:00Z",
                ),
                (
                    "https://www.anthropic.com/news/second-story",
                    "2026-07-29T16:30:00Z",
                ),
            ],
            entries,
        )

    def test_sitemap_index_and_relative_locations(self) -> None:
        entries = parse_sitemap(
            fixture("sitemap_index.xml"), "https://www.anthropic.com/"
        )

        self.assertEqual(
            [
                (
                    "https://www.anthropic.com/sitemaps/news.xml",
                    "2026-07-31T12:00:00Z",
                ),
                ("https://www.anthropic.com/sitemaps/pages.xml", None),
            ],
            entries,
        )

    def test_unsafe_sitemap_xml_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "entities are not allowed"):
            parse_sitemap(fixture("malicious_rss.xml"), "https://openai.com/")


class ArticlePageParserTests(unittest.TestCase):
    def test_claude_blogposting_json_ld_takes_priority(self) -> None:
        article = parse_article_page(
            source("claude_blog"),
            fixture("claude_article.html"),
            "https://claude.com/blog/build-reliable-agents?ref=test",
            FETCHED_AT,
        )

        self.assertEqual("https://claude.com/blog/build-reliable-agents", article.url)
        self.assertEqual("Build reliable agents with Claude", article.title)
        self.assertEqual(
            "A deterministic guide to reliable agents.", article.summary
        )
        self.assertEqual("Ada Builder, Anthropic", article.author)
        self.assertEqual("Engineering", article.category)
        self.assertEqual("2026-07-24T16:30:00Z", article.published_at)
        self.assertEqual("2026-07-25T18:00:00Z", article.modified_at)

    def test_anthropic_open_graph_and_meta_fallback(self) -> None:
        article = parse_article_page(
            source("anthropic_news"),
            fixture("anthropic_article.html"),
            "https://www.anthropic.com/news/investigating-incidents",
            FETCHED_AT,
        )

        self.assertEqual(
            "https://www.anthropic.com/news/investigating-incidents", article.url
        )
        self.assertEqual("Investigating real-world incidents", article.title)
        self.assertEqual(
            "What we learned from three & carefully studied incidents.",
            article.summary,
        )
        self.assertEqual("Anthropic Frontier Red Team", article.author)
        self.assertEqual("Frontier Red Team", article.category)
        self.assertEqual("2026-07-30T15:00:00Z", article.published_at)
        self.assertEqual("2026-07-31T10:00:00Z", article.modified_at)

    def test_openai_h1_meta_and_plain_date_fallback(self) -> None:
        article = parse_article_page(
            source("openai_developers"),
            fixture("openai_article.html"),
            "https://developers.openai.com/blog/custom-code-review-rules-for-codex",
            FETCHED_AT,
        )

        self.assertEqual(
            "https://developers.openai.com/blog/custom-code-review-rules-for-codex",
            article.url,
        )
        self.assertEqual("Custom Code Review rules for Codex", article.title)
        self.assertEqual(
            "More precise reviews without an LLM parsing step.", article.summary
        )
        self.assertEqual("Codex", article.category)
        self.assertEqual("2026-07-20T00:00:00Z", article.published_at)

    def test_cursor_blog_json_ld_metadata(self) -> None:
        article = parse_article_page(
            source("cursor_blog"),
            fixture("cursor_article.html"),
            "https://cursor.com/blog/git-at-any-scale",
            FETCHED_AT,
        )

        self.assertEqual("https://cursor.com/blog/git-at-any-scale", article.url)
        self.assertEqual("Git at any scale", article.title)
        self.assertEqual(
            "How Cursor handles monorepos with millions of files.", article.summary
        )
        self.assertEqual("Vicent Martí", article.author)
        self.assertEqual("Research", article.category)
        self.assertEqual("2026-08-18T12:00:00Z", article.published_at)
        self.assertEqual("2026-08-19T10:00:00Z", article.modified_at)

    def test_cursor_blog_cn_official_locale(self) -> None:
        article = parse_article_page(
            source("cursor_blog"),
            fixture("cursor_article_cn.html"),
            "https://cursor.com/cn/blog/git-at-any-scale",
            FETCHED_AT,
        )

        self.assertEqual("https://cursor.com/blog/git-at-any-scale", article.url)
        self.assertEqual("任意规模的 Git", article.title)
        self.assertEqual(
            "Cursor 如何处理包含数百万文件的大型仓库。", article.summary
        )
        self.assertEqual("Vicent Martí", article.author)
        self.assertEqual("研究", article.category)
        self.assertEqual("2026-08-18T12:00:00Z", article.published_at)
        self.assertEqual("2026-08-19T10:00:00Z", article.modified_at)

    def test_external_article_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the configured publisher"):
            parse_article_page(
                source("claude_blog"),
                fixture("claude_article.html"),
                "https://evil.example/blog/copied",
                FETCHED_AT,
            )


if __name__ == "__main__":
    unittest.main()
