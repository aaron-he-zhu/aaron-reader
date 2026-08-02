from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SourceConfig:
    slug: str
    name: str
    home_url: str
    fetch_url: str
    adapter: str
    history_limit: int = 50
    enabled: bool = True
    sitemap_url: str = ""
    sitemap_prefix: str = ""
    sitemap_interval_hours: int = 24
    metadata_url: str = ""


@dataclass(frozen=True)
class AIModelPrice:
    """Optional user-supplied USD prices per one million tokens.

    Aaron Reader deliberately does not bake mutable provider pricing into the
    program.  Token/request caps work without this table; monetary caps are
    enforced only when the selected model has an explicit price snapshot.
    """

    input_usd_per_million: float
    output_usd_per_million: float
    cached_input_usd_per_million: float = 0.0
    cache_write_input_usd_per_million: float = 0.0


@dataclass(frozen=True)
class AIBudgetConfig:
    timezone: str = "America/Los_Angeles"
    daily_max_requests: int = 20
    daily_max_total_tokens: int = 30_000
    daily_max_cost_usd: float = 0.0
    monthly_max_requests: int = 300
    monthly_max_total_tokens: int = 400_000
    monthly_max_cost_usd: float = 0.0


@dataclass(frozen=True)
class AIBatchConfig:
    enabled: bool = False
    max_articles_per_run: int = 10
    concurrency: int = 1
    max_attempts: int = 2


@dataclass(frozen=True)
class AIConfig:
    """Explicitly opt-in settings for token-consuming enrichment."""

    enabled: bool = False
    provider: str = "deepseek"
    translation_model: str = "deepseek-v4-flash"
    summary_model: str = "deepseek-v4-flash"
    digest_model: str = "deepseek-v4-flash"
    reasoning_effort: str = "none"
    store: bool = False
    api_key_environment: str = "DEEPSEEK_API_KEY"
    input_policy: str = "metadata_only"
    max_input_chars_per_article: int = 12_000
    max_full_text_chars: int = 60_000
    max_output_tokens_summary: int = 400
    max_output_tokens_translation: int = 800
    max_output_tokens_digest: int = 1_200
    timeout_seconds: int = 60
    max_response_bytes: int = 2_000_000
    summary_enabled: bool = True
    translation_enabled: bool = True
    digest_enabled: bool = True
    full_text_enabled: bool = False
    budget: AIBudgetConfig = field(default_factory=AIBudgetConfig)
    batch: AIBatchConfig = field(default_factory=AIBatchConfig)
    prices: Dict[str, AIModelPrice] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    sources: List[SourceConfig]
    default_language: str = "en"
    database_path: str = "data/reader.sqlite3"
    output_dir: str = "public"
    request_timeout_seconds: int = 25
    max_response_bytes: int = 8_000_000
    ai: AIConfig = field(default_factory=AIConfig)


@dataclass
class ArticleCandidate:
    source_slug: str
    external_id: str
    url: str
    title: str
    summary: str = ""
    author: str = ""
    category: str = ""
    published_at: Optional[str] = None
    modified_at: Optional[str] = None
    content_hash: str = ""


@dataclass
class FetchResult:
    url: str
    status: int
    body: bytes = b""
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    body_hash: str = ""
    not_modified: bool = False


@dataclass
class SourceSyncResult:
    source_slug: str
    status: str
    http_status: Optional[int] = None
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    unread_new: int = 0
    seeded: int = 0
    error: str = ""
    warning: str = ""


@dataclass
class SyncResult:
    sources: List[SourceSyncResult] = field(default_factory=list)

    @property
    def failed(self) -> List[SourceSyncResult]:
        return [
            result for result in self.sources if result.status in ("error", "degraded")
        ]

    @property
    def unread_new(self) -> int:
        return sum(result.unread_new for result in self.sources)

    @property
    def source_counts(self) -> Dict[str, int]:
        return {
            result.source_slug: result.unread_new
            for result in self.sources
            if result.unread_new
        }
