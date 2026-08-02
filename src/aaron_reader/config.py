import json
import math
from pathlib import Path
from typing import Dict, Mapping, Optional

from .ai_profiles import (
    DEFAULT_AI_FALLBACK_PROVIDER,
    DEFAULT_AI_PROVIDER,
    ai_provider_profile,
)
from .i18n import normalize_language
from .models import (
    AIConfig,
    AIBatchConfig,
    AIBudgetConfig,
    AIModelPrice,
    AppConfig,
    SourceConfig,
)


SUPPORTED_ADAPTERS = {
    "rss",
    "openai_developers",
    "claude_blog",
    "anthropic_news",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(config_path: Optional[str] = None) -> AppConfig:
    path = Path(config_path).expanduser() if config_path else project_root() / "config" / "sources.json"
    if not path.is_absolute():
        path = project_root() / path
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("config.sources must be a non-empty list")

    sources = []
    seen_slugs = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ValueError("each source must be an object")
        source = SourceConfig(
            slug=str(raw.get("slug", "")).strip(),
            name=str(raw.get("name", "")).strip(),
            home_url=str(raw.get("home_url", "")).strip(),
            fetch_url=str(raw.get("fetch_url", "")).strip(),
            adapter=str(raw.get("adapter", "")).strip(),
            history_limit=int(raw.get("history_limit", 50)),
            enabled=bool(raw.get("enabled", True)),
            sitemap_url=str(raw.get("sitemap_url", "")).strip(),
            sitemap_prefix=str(raw.get("sitemap_prefix", "")).strip(),
            sitemap_interval_hours=int(raw.get("sitemap_interval_hours", 24)),
            metadata_url=str(raw.get("metadata_url", "")).strip(),
        )
        if not source.slug or not source.name or not source.fetch_url:
            raise ValueError("source slug, name, and fetch_url are required")
        if source.slug in seen_slugs:
            raise ValueError("duplicate source slug: %s" % source.slug)
        if source.adapter not in SUPPORTED_ADAPTERS:
            raise ValueError("unsupported adapter for %s: %s" % (source.slug, source.adapter))
        if source.history_limit < 1 or source.history_limit > 500:
            raise ValueError("history_limit for %s must be between 1 and 500" % source.slug)
        if bool(source.sitemap_url) != bool(source.sitemap_prefix):
            raise ValueError("%s must set both sitemap_url and sitemap_prefix" % source.slug)
        if source.sitemap_interval_hours < 1 or source.sitemap_interval_hours > 720:
            raise ValueError("sitemap_interval_hours for %s must be between 1 and 720" % source.slug)
        seen_slugs.add(source.slug)
        sources.append(source)

    return AppConfig(
        sources=sources,
        default_language=normalize_language(payload.get("default_language", "en")),
        database_path=str(payload.get("database_path", "data/reader.sqlite3")),
        output_dir=str(payload.get("output_dir", "public")),
        request_timeout_seconds=int(payload.get("request_timeout_seconds", 25)),
        max_response_bytes=int(payload.get("max_response_bytes", 8_000_000)),
        ai=_load_ai_config(payload.get("ai")),
    )


def _object(value: object, name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % name)
    return value


def _integer(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 10_000_000,
) -> int:
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % name)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s must be an integer" % name) from exc
    if result < minimum or result > maximum:
        raise ValueError("%s must be between %d and %d" % (name, minimum, maximum))
    return result


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError("%s must be a number" % name)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a number" % name) from exc
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % name)
    if result < minimum:
        raise ValueError("%s must be at least %s" % (name, minimum))
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("%s must be a JSON boolean" % name)
    return value


def _nonempty(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError("%s must be a non-empty string" % name)
    return result


def _fixed_provider_model(value: object, name: str, expected_model: str) -> str:
    result = _nonempty(value, name)
    if result != expected_model:
        raise ValueError("%s must be %s" % (name, expected_model))
    return result


def _load_ai_config(raw_value: object) -> AIConfig:
    raw = _object(raw_value, "config.ai")
    features = _object(raw.get("features"), "config.ai.features")
    outputs = _object(raw.get("max_output_tokens"), "config.ai.max_output_tokens")
    budgets = _object(raw.get("budget"), "config.ai.budget")
    batch = _object(raw.get("batch"), "config.ai.batch")

    provider = _nonempty(
        raw.get("provider", DEFAULT_AI_PROVIDER),
        "config.ai.provider",
    )
    try:
        profile = ai_provider_profile(provider)
    except ValueError as exc:
        raise ValueError(
            "config.ai.provider must be 'deepseek' or 'openrouter'"
        ) from exc
    fallback_default = (
        DEFAULT_AI_FALLBACK_PROVIDER if provider == DEFAULT_AI_PROVIDER else ""
    )
    fallback_provider = str(
        raw.get("fallback_provider", fallback_default) or ""
    ).strip()
    if fallback_provider:
        try:
            ai_provider_profile(fallback_provider)
        except ValueError as exc:
            raise ValueError(
                "config.ai.fallback_provider must be empty or 'deepseek'"
            ) from exc
        if not (
            provider == DEFAULT_AI_PROVIDER
            and fallback_provider == DEFAULT_AI_FALLBACK_PROVIDER
        ):
            raise ValueError(
                "automatic AI fallback only supports openrouter -> deepseek"
            )
    reasoning = _nonempty(
        raw.get("reasoning_effort", "none"), "config.ai.reasoning_effort"
    )
    if reasoning != "none":
        raise ValueError(
            "config.ai.reasoning_effort must be 'none'; cloud reasoning is disabled"
        )
    input_policy = _nonempty(
        raw.get("input_policy", "metadata_only"), "config.ai.input_policy"
    )
    if input_policy not in {
        "metadata_only",
        "fetch_on_demand_ephemeral",
        "fetch_on_demand_cached_local",
    }:
        raise ValueError("unsupported config.ai.input_policy: %s" % input_policy)
    api_key_environment = _nonempty(
        raw.get("api_key_environment", profile.api_key_environment),
        "config.ai.api_key_environment",
    )
    if api_key_environment != profile.api_key_environment:
        raise ValueError(
            "config.ai.api_key_environment must be %s for provider %s"
            % (profile.api_key_environment, profile.provider)
        )
    store = _boolean(raw.get("store", False), "config.ai.store")
    if store:
        raise ValueError("config.ai.store must remain false for independent enrichment calls")

    price_payload = _object(raw.get("prices"), "config.ai.prices")
    prices: Dict[str, AIModelPrice] = {}
    for model, raw_price in price_payload.items():
        model_name = _nonempty(model, "config.ai.prices model")
        price = _object(raw_price, "config.ai.prices.%s" % model_name)
        input_rate = _number(
            price.get("input_usd_per_million"),
            "config.ai.prices.%s.input_usd_per_million" % model_name,
        )
        prices[model_name] = AIModelPrice(
            input_usd_per_million=input_rate,
            output_usd_per_million=_number(
                price.get("output_usd_per_million"),
                "config.ai.prices.%s.output_usd_per_million" % model_name,
            ),
            cached_input_usd_per_million=_number(
                price.get("cached_input_usd_per_million"),
                "config.ai.prices.%s.cached_input_usd_per_million" % model_name,
            ),
            cache_write_input_usd_per_million=_number(
                price.get("cache_write_input_usd_per_million"),
                "config.ai.prices.%s.cache_write_input_usd_per_million" % model_name,
            ),
        )

    budget = AIBudgetConfig(
        timezone=_nonempty(
            budgets.get("timezone", "America/Los_Angeles"),
            "config.ai.budget.timezone",
        ),
        daily_max_requests=_integer(
            budgets.get("daily_max_requests", 20),
            "config.ai.budget.daily_max_requests",
            maximum=1_000_000,
        ),
        daily_max_total_tokens=_integer(
            budgets.get("daily_max_total_tokens", 30_000),
            "config.ai.budget.daily_max_total_tokens",
        ),
        daily_max_cost_usd=_number(
            budgets.get("daily_max_cost_usd", 0),
            "config.ai.budget.daily_max_cost_usd",
        ),
        monthly_max_requests=_integer(
            budgets.get("monthly_max_requests", 300),
            "config.ai.budget.monthly_max_requests",
            maximum=1_000_000,
        ),
        monthly_max_total_tokens=_integer(
            budgets.get("monthly_max_total_tokens", 400_000),
            "config.ai.budget.monthly_max_total_tokens",
        ),
        monthly_max_cost_usd=_number(
            budgets.get("monthly_max_cost_usd", 0),
            "config.ai.budget.monthly_max_cost_usd",
        ),
    )
    batch_config = AIBatchConfig(
        enabled=_boolean(batch.get("enabled", False), "config.ai.batch.enabled"),
        max_articles_per_run=_integer(
            batch.get("max_articles_per_run", 10),
            "config.ai.batch.max_articles_per_run",
            minimum=1,
            maximum=1_000,
        ),
        concurrency=_integer(
            batch.get("concurrency", 1),
            "config.ai.batch.concurrency",
            minimum=1,
            maximum=1,
        ),
        max_attempts=_integer(
            batch.get("max_attempts", 2),
            "config.ai.batch.max_attempts",
            minimum=1,
            maximum=5,
        ),
    )

    return AIConfig(
        enabled=_boolean(raw.get("enabled", False), "config.ai.enabled"),
        provider=provider,
        fallback_provider=fallback_provider,
        translation_model=_fixed_provider_model(
            raw.get("translation_model", profile.model),
            "config.ai.translation_model",
            profile.model,
        ),
        summary_model=_fixed_provider_model(
            raw.get("summary_model", profile.model),
            "config.ai.summary_model",
            profile.model,
        ),
        digest_model=_fixed_provider_model(
            raw.get("digest_model", profile.model),
            "config.ai.digest_model",
            profile.model,
        ),
        reasoning_effort=reasoning,
        store=store,
        api_key_environment=api_key_environment,
        input_policy=input_policy,
        max_input_chars_per_article=_integer(
            raw.get("max_input_chars_per_article", 12_000),
            "config.ai.max_input_chars_per_article",
            minimum=500,
            maximum=200_000,
        ),
        max_full_text_chars=_integer(
            raw.get("max_full_text_chars", 60_000),
            "config.ai.max_full_text_chars",
            minimum=1_000,
            maximum=1_000_000,
        ),
        max_output_tokens_summary=_integer(
            outputs.get("summary", 400),
            "config.ai.max_output_tokens.summary",
            minimum=32,
            maximum=16_000,
        ),
        max_output_tokens_translation=_integer(
            outputs.get("translation", 800),
            "config.ai.max_output_tokens.translation",
            minimum=32,
            maximum=32_000,
        ),
        max_output_tokens_digest=_integer(
            outputs.get("digest", 1_200),
            "config.ai.max_output_tokens.digest",
            minimum=32,
            maximum=32_000,
        ),
        timeout_seconds=_integer(
            raw.get("timeout_seconds", 60),
            "config.ai.timeout_seconds",
            minimum=5,
            maximum=600,
        ),
        max_response_bytes=_integer(
            raw.get("max_response_bytes", 2_000_000),
            "config.ai.max_response_bytes",
            minimum=1_024,
            maximum=2_097_152,
        ),
        summary_enabled=_boolean(
            features.get("summary", True), "config.ai.features.summary"
        ),
        translation_enabled=_boolean(
            features.get("translation", True), "config.ai.features.translation"
        ),
        digest_enabled=_boolean(
            features.get("digest", True), "config.ai.features.digest"
        ),
        full_text_enabled=_boolean(
            features.get("full_text", False), "config.ai.features.full_text"
        ),
        budget=budget,
        batch=batch_config,
        prices=prices,
    )


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root() / path
