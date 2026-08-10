"""Closed AI provider profiles allowed by Aaron Reader cloud automation."""

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple


@dataclass(frozen=True)
class AIProviderProfile:
    provider: str
    model: str
    api_key_environment: str


DEEPSEEK_PROFILE = AIProviderProfile(
    provider="deepseek",
    model="deepseek-v4-flash",
    api_key_environment="DEEPSEEK_API_KEY",
)
OPENROUTER_PROFILE = AIProviderProfile(
    provider="openrouter",
    model="openrouter/free",
    api_key_environment="OPENROUTER_API_KEY",
)

AI_PROVIDER_PROFILES: Dict[str, AIProviderProfile] = {
    profile.provider: profile
    for profile in (DEEPSEEK_PROFILE, OPENROUTER_PROFILE)
}
SUPPORTED_AI_PROVIDERS: Tuple[str, ...] = tuple(AI_PROVIDER_PROFILES)
DEFAULT_AI_PROVIDER = OPENROUTER_PROFILE.provider
DEFAULT_AI_FALLBACK_PROVIDER = DEEPSEEK_PROFILE.provider


def ai_provider_profile(provider: object) -> AIProviderProfile:
    name = str(provider or "").strip()
    try:
        return AI_PROVIDER_PROFILES[name]
    except KeyError as exc:
        raise ValueError("unsupported AI provider: %s" % (name or "<empty>")) from exc


def matches_ai_provider_profile(
    provider: object,
    models: Iterable[object],
    api_key_environment: object,
) -> bool:
    try:
        profile = ai_provider_profile(provider)
    except ValueError:
        return False
    return bool(
        str(api_key_environment or "").strip() == profile.api_key_environment
        and all(str(model or "").strip() == profile.model for model in models)
    )


__all__ = [
    "AIProviderProfile",
    "AI_PROVIDER_PROFILES",
    "DEFAULT_AI_FALLBACK_PROVIDER",
    "DEFAULT_AI_PROVIDER",
    "DEEPSEEK_PROFILE",
    "OPENROUTER_PROFILE",
    "SUPPORTED_AI_PROVIDERS",
    "ai_provider_profile",
    "matches_ai_provider_profile",
]
