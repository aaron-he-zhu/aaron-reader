import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.cli import build_parser, main  # noqa: E402


class StatusCliTests(unittest.TestCase):
    def test_default_help_is_canonical_english(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("deterministic blog reader", help_text)
        self.assertIn("Interface language", help_text)
        self.assertNotIn("本地", help_text)

    def test_simplified_chinese_help_is_available(self) -> None:
        help_text = build_parser("zh-CN").format_help()
        self.assertIn("确定性", help_text)
        self.assertNotIn("本地", help_text)
        self.assertIn("界面语言", help_text)

    def test_status_can_switch_languages_before_or_after_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "reader.sqlite3")
            english_stdout = io.StringIO()
            chinese_stdout = io.StringIO()
            with redirect_stdout(english_stdout):
                english = main(["--database", database, "status"])
            with redirect_stdout(chinese_stdout):
                chinese = main(
                    ["--database", database, "status", "--language", "zh-CN"]
                )
        self.assertEqual(0, english)
        self.assertEqual(0, chinese)
        self.assertIn("Total 0 · Unread 0 · Starred 0", english_stdout.getvalue())
        self.assertIn("总计 0 · 未读 0 · 收藏 0", chinese_stdout.getvalue())

    def test_environment_language_is_used_without_cli_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "reader.sqlite3")
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"AARON_READER_LANG": "zh-CN"}):
                with redirect_stdout(output):
                    result = main(["--database", database, "status"])
        self.assertEqual(0, result)
        self.assertIn("总计 0", output.getvalue())

    def test_strict_status_rejects_never_synced_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "reader.sqlite3")
            with redirect_stdout(io.StringIO()):
                normal = main(["--database", database, "status"])
                strict = main(["--database", database, "status", "--strict"])
        self.assertEqual(0, normal)
        self.assertEqual(1, strict)

    def test_crawler_handoff_commands_have_stable_automation_syntax(self) -> None:
        imported = build_parser().parse_args(
            ["crawl-import", "crawler/latest.json"]
        )
        self.assertEqual("crawl-import", imported.command)
        self.assertEqual("crawler/latest.json", imported.path)
        self.assertFalse(imported.seed)

        seeded = build_parser().parse_args(
            ["crawl-import", "crawler/latest.json", "--seed", "--json"]
        )
        self.assertTrue(seeded.seed)
        self.assertTrue(seeded.json)

        exported = build_parser().parse_args(
            ["crawl-export", "crawler/latest.json", "--json"]
        )
        self.assertEqual("crawl-export", exported.command)
        self.assertEqual("crawler/latest.json", exported.path)


if __name__ == "__main__":
    unittest.main()
