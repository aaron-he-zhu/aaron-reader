import json
import sys
import tempfile
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.config import load_config  # noqa: E402
from aaron_reader.models import AIConfig  # noqa: E402


def base_payload():
    return {
        "sources": [
            {
                "slug": "example",
                "name": "Example",
                "home_url": "https://example.com/blog",
                "fetch_url": "https://example.com/feed.xml",
                "adapter": "rss",
            }
        ],
        "ai": {},
    }


class AIConfigTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(str(path))

    def test_default_ai_configuration_is_openrouter_with_deepseek_fallback(self):
        configurations = (
            AIConfig(),
            self.load(base_payload()).ai,
            load_config(str(REPOSITORY_ROOT / "config" / "sources.json")).ai,
        )
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                self.assertFalse(configuration.enabled)
                self.assertEqual("openrouter", configuration.provider)
                self.assertEqual("deepseek", configuration.fallback_provider)
                self.assertEqual("openrouter/free", configuration.translation_model)
                self.assertEqual("openrouter/free", configuration.summary_model)
                self.assertEqual("openrouter/free", configuration.digest_model)
                self.assertEqual("none", configuration.reasoning_effort)
                self.assertEqual("OPENROUTER_API_KEY", configuration.api_key_environment)
                self.assertEqual(
                    "America/Los_Angeles", configuration.budget.timezone
                )

    def test_production_generates_article_translations_without_article_summaries(self):
        production = load_config(
            str(REPOSITORY_ROOT / "config" / "sources.json")
        ).ai

        self.assertFalse(production.summary_enabled)
        self.assertTrue(production.translation_enabled)
        self.assertTrue(production.digest_enabled)

    def test_exact_openrouter_free_profile_is_accepted(self):
        payload = base_payload()
        payload["ai"].update(
            {
                "provider": "openrouter",
                "translation_model": "openrouter/free",
                "summary_model": "openrouter/free",
                "digest_model": "openrouter/free",
                "reasoning_effort": "none",
                "api_key_environment": "OPENROUTER_API_KEY",
            }
        )

        configuration = self.load(payload).ai

        self.assertEqual("openrouter", configuration.provider)
        self.assertEqual("openrouter/free", configuration.translation_model)
        self.assertEqual("openrouter/free", configuration.summary_model)
        self.assertEqual("openrouter/free", configuration.digest_model)
        self.assertEqual("none", configuration.reasoning_effort)
        self.assertEqual(
            "OPENROUTER_API_KEY",
            configuration.api_key_environment,
        )

    def test_explicit_deepseek_profile_is_deepseek_only(self):
        payload = base_payload()
        payload["ai"].update(
            {
                "provider": "deepseek",
                "translation_model": "deepseek-v4-flash",
                "summary_model": "deepseek-v4-flash",
                "digest_model": "deepseek-v4-flash",
                "reasoning_effort": "none",
                "api_key_environment": "DEEPSEEK_API_KEY",
            }
        )

        configuration = self.load(payload).ai

        self.assertEqual("deepseek", configuration.provider)
        self.assertEqual("", configuration.fallback_provider)
        self.assertEqual("deepseek-v4-flash", configuration.digest_model)
        self.assertEqual("DEEPSEEK_API_KEY", configuration.api_key_environment)

    def test_unsupported_or_mismatched_fixed_profiles_are_rejected(self):
        deepseek_profile = {
            "provider": "deepseek",
            "translation_model": "deepseek-v4-flash",
            "summary_model": "deepseek-v4-flash",
            "digest_model": "deepseek-v4-flash",
            "reasoning_effort": "none",
            "api_key_environment": "DEEPSEEK_API_KEY",
        }
        openrouter_profile = {
            "provider": "openrouter",
            "translation_model": "openrouter/free",
            "summary_model": "openrouter/free",
            "digest_model": "openrouter/free",
            "reasoning_effort": "none",
            "api_key_environment": "OPENROUTER_API_KEY",
        }
        invalid_profiles = (
            {**deepseek_profile, "provider": "openai"},
            {**deepseek_profile, "summary_model": "deepseek-v4-pro"},
            {**deepseek_profile, "translation_model": "openrouter/free"},
            {**deepseek_profile, "digest_model": "another-model"},
            {**deepseek_profile, "reasoning_effort": "low"},
            {**deepseek_profile, "api_key_environment": "OPENROUTER_API_KEY"},
            {**deepseek_profile, "fallback_provider": "deepseek"},
            {**deepseek_profile, "fallback_provider": "openrouter"},
            {**openrouter_profile, "summary_model": "deepseek-v4-flash"},
            {**openrouter_profile, "translation_model": "openrouter/auto"},
            {**openrouter_profile, "digest_model": "another-model"},
            {**openrouter_profile, "reasoning_effort": "low"},
            {**openrouter_profile, "api_key_environment": "DEEPSEEK_API_KEY"},
            {**openrouter_profile, "fallback_provider": "openrouter"},
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                payload = base_payload()
                payload["ai"].update(profile)
                with self.assertRaises(ValueError):
                    self.load(payload)

    def test_all_ai_opt_in_switches_require_real_json_booleans(self):
        mutations = (
            (("enabled",), "false"),
            (("store",), "false"),
            (("batch", "enabled"), "false"),
            (("features", "summary"), "true"),
            (("features", "translation"), 1),
            (("features", "digest"), 0),
            (("features", "full_text"), "false"),
        )
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                payload = base_payload()
                target = payload["ai"]
                for key in path[:-1]:
                    target = target.setdefault(key, {})
                target[path[-1]] = value
                with self.assertRaisesRegex(ValueError, "JSON boolean"):
                    self.load(payload)

    def test_price_snapshot_requires_all_finite_rates(self):
        valid = {
            "input_usd_per_million": 1.0,
            "output_usd_per_million": 2.0,
            "cached_input_usd_per_million": 0.5,
            "cache_write_input_usd_per_million": 1.25,
        }
        payload = base_payload()
        payload["ai"]["prices"] = {"example-model": dict(valid)}
        price = self.load(payload).ai.prices["example-model"]
        self.assertEqual(1.25, price.cache_write_input_usd_per_million)

        for missing in valid:
            with self.subTest(missing=missing):
                payload = base_payload()
                incomplete = dict(valid)
                incomplete.pop(missing)
                payload["ai"]["prices"] = {"example-model": incomplete}
                with self.assertRaises(ValueError):
                    self.load(payload)

        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=nonfinite):
                payload = base_payload()
                invalid = dict(valid)
                invalid["cached_input_usd_per_million"] = nonfinite
                payload["ai"]["prices"] = {"example-model": invalid}
                with self.assertRaisesRegex(ValueError, "finite"):
                    self.load(payload)

    def test_batch_worker_is_explicitly_single_threaded(self):
        payload = base_payload()
        payload["ai"]["batch"] = {"concurrency": 2}
        with self.assertRaisesRegex(ValueError, "between 1 and 1"):
            self.load(payload)


if __name__ == "__main__":
    unittest.main()
