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

    def test_default_ai_configuration_is_fixed_deepseek_nonthinking(self):
        configurations = (
            AIConfig(),
            self.load(base_payload()).ai,
            load_config(str(REPOSITORY_ROOT / "config" / "sources.json")).ai,
        )
        for configuration in configurations:
            with self.subTest(configuration=configuration):
                self.assertFalse(configuration.enabled)
                self.assertEqual("deepseek", configuration.provider)
                self.assertEqual("deepseek-v4-flash", configuration.translation_model)
                self.assertEqual("deepseek-v4-flash", configuration.summary_model)
                self.assertEqual("deepseek-v4-flash", configuration.digest_model)
                self.assertEqual("none", configuration.reasoning_effort)
                self.assertEqual("DEEPSEEK_API_KEY", configuration.api_key_environment)
                self.assertEqual(
                    "America/Los_Angeles", configuration.budget.timezone
                )

    def test_openai_and_mutable_deepseek_profiles_are_rejected(self):
        invalid_fields = (
            ("provider", "openai"),
            ("summary_model", "deepseek-v4-pro"),
            ("translation_model", "gpt-5.6-luna"),
            ("digest_model", "another-model"),
            ("reasoning_effort", "low"),
            ("api_key_environment", "OPENAI_API_KEY"),
        )
        for field, value in invalid_fields:
            with self.subTest(field=field):
                payload = base_payload()
                payload["ai"][field] = value
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
