import json
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.i18n import (  # noqa: E402
    SUPPORTED_LANGUAGES,
    normalize_language,
    resolve_language,
    translate,
)
from aaron_reader.config import load_config  # noqa: E402


class I18nTests(unittest.TestCase):
    def test_aliases_are_canonicalized(self) -> None:
        self.assertEqual(("en", "zh-CN"), SUPPORTED_LANGUAGES)
        for alias in ("en", "EN_us", "english"):
            self.assertEqual("en", normalize_language(alias))
        for alias in ("zh", "zh_CN", "zh-Hans", "simplified_chinese"):
            self.assertEqual("zh-CN", normalize_language(alias))
        with self.assertRaisesRegex(ValueError, "unsupported language"):
            normalize_language("fr")

    def test_resolution_precedence(self) -> None:
        environment = {"AARON_READER_LANG": "zh"}
        self.assertEqual("en", resolve_language("en", "zh-CN", environment))
        self.assertEqual("zh-CN", resolve_language(None, "en", environment))
        self.assertEqual("zh-CN", resolve_language(None, "zh-CN", {}))
        self.assertEqual("en", resolve_language(None, None, {}))

    def test_translation_fallback_and_formatting(self) -> None:
        self.assertEqual("Healthy", translate("health.healthy", "en").title())
        self.assertEqual("正常", translate("health.healthy", "zh-CN"))
        self.assertEqual("missing.key", translate("missing.key", "zh-CN"))
        with self.assertRaisesRegex(ValueError, "invalid translation parameters"):
            translate("cli.sync.summary", "en")

    def test_real_environment_is_not_required(self) -> None:
        with mock.patch.dict(os.environ, {"AARON_READER_LANG": "zh-CN"}, clear=True):
            self.assertEqual("zh-CN", resolve_language())

    def test_configuration_language_is_validated_and_canonicalized(self) -> None:
        payload = {
            "default_language": "zh_CN",
            "sources": [
                {
                    "slug": "example",
                    "name": "Example",
                    "home_url": "https://example.com/",
                    "fetch_url": "https://example.com/feed.xml",
                    "adapter": "rss",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sources.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual("zh-CN", load_config(str(path)).default_language)
            payload["default_language"] = "fr"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported language"):
                load_config(str(path))


if __name__ == "__main__":
    unittest.main()
