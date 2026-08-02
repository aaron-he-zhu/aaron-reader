import sys
from pathlib import Path
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from aaron_reader.notifier import notify_new_articles  # noqa: E402


class NotifierTests(unittest.TestCase):
    @mock.patch("aaron_reader.notifier.platform.system", return_value="Darwin")
    @mock.patch("aaron_reader.notifier.subprocess.run")
    def test_english_notification_is_the_default(self, run, _system) -> None:
        run.return_value.returncode = 0

        delivered = notify_new_articles(3, {"openai": 2, "anthropic": 1})

        self.assertTrue(delivered)
        script = run.call_args.args[0][2]
        self.assertIn(
            "Found 3 new article(s) (anthropic 1, openai 2)",
            script,
        )

    @mock.patch("aaron_reader.notifier.platform.system", return_value="Darwin")
    @mock.patch("aaron_reader.notifier.subprocess.run")
    def test_chinese_notification_can_be_selected(self, run, _system) -> None:
        run.return_value.returncode = 0

        delivered = notify_new_articles(
            3,
            {"openai": 2, "anthropic": 1},
            language="zh-CN",
        )

        self.assertTrue(delivered)
        script = run.call_args.args[0][2]
        self.assertIn("发现 3 篇新文章（anthropic 1，openai 2）", script)

    @mock.patch("aaron_reader.notifier.platform.system", return_value="Linux")
    @mock.patch("aaron_reader.notifier.subprocess.run")
    def test_unsupported_platform_does_not_invoke_osascript(self, run, _system) -> None:
        self.assertFalse(notify_new_articles(1, {"openai": 1}))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
