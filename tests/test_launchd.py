import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY_ROOT / "scripts"


def environment_with_language(language: Optional[str]) -> Dict[str, str]:
    environment = os.environ.copy()
    if language is None:
        environment.pop("AARON_READER_LANG", None)
    else:
        environment["AARON_READER_LANG"] = language
    return environment


class RuntimeInstallerTests(unittest.TestCase):
    def test_runtime_is_refreshed_but_the_installed_database_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            runtime = root / "Library" / "Application Support" / "Aaron Reader"
            (source / "config").mkdir(parents=True)
            (source / "src" / "aaron_reader").mkdir(parents=True)
            (source / "data").mkdir(parents=True)
            entrypoint = source / "aaron-reader"
            entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            entrypoint.chmod(0o755)
            (source / "config" / "sources.json").write_text(
                '{"sources": []}', encoding="utf-8"
            )
            module = source / "src" / "aaron_reader" / "__main__.py"
            module.write_text("VERSION = 1\n", encoding="utf-8")
            with sqlite3.connect(str(source / "data" / "reader.sqlite3")) as connection:
                connection.execute("CREATE TABLE marker(value INTEGER NOT NULL)")
                connection.execute("INSERT INTO marker(value) VALUES(1)")

            command = [
                sys.executable,
                str(SCRIPTS / "install_runtime.py"),
                "--source-root",
                str(source),
                "--runtime-root",
                str(runtime),
            ]
            first = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertTrue((runtime / "aaron-reader").stat().st_mode & 0o100)
            self.assertEqual(
                str(source.resolve()),
                (runtime / ".source-root").read_text(encoding="utf-8").strip(),
            )
            with sqlite3.connect(str(runtime / "data" / "reader.sqlite3")) as connection:
                self.assertEqual(1, connection.execute("SELECT value FROM marker").fetchone()[0])

            module.write_text("VERSION = 2\n", encoding="utf-8")
            with sqlite3.connect(str(source / "data" / "reader.sqlite3")) as connection:
                connection.execute("UPDATE marker SET value=2")
            second = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(
                "VERSION = 2\n",
                (runtime / "src" / "aaron_reader" / "__main__.py").read_text(
                    encoding="utf-8"
                ),
            )
            with sqlite3.connect(str(runtime / "data" / "reader.sqlite3")) as connection:
                self.assertEqual(1, connection.execute("SELECT value FROM marker").fetchone()[0])


class LaunchdRendererTests(unittest.TestCase):
    def test_renderer_emits_valid_absolute_source_tree_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            reader_root = temporary_root / "Reader Root With Spaces"
            (reader_root / "config").mkdir(parents=True)
            (reader_root / "src" / "aaron_reader").mkdir(parents=True)
            (reader_root / "config" / "sources.json").write_text(
                '{"sources": []}', encoding="utf-8"
            )
            (reader_root / "src" / "aaron_reader" / "__main__.py").write_text(
                "", encoding="utf-8"
            )
            entrypoint = reader_root / "aaron-reader"
            entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            entrypoint.chmod(0o755)
            output = temporary_root / "job.plist"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "render_launchd.py"),
                    "--root",
                    str(reader_root),
                    "--output",
                    str(output),
                    "--interval",
                    "1800",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment_with_language("zh-CN"),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            with output.open("rb") as handle:
                payload = plistlib.load(handle)

            resolved_root = reader_root.resolve()
            self.assertEqual("com.aaron.reader", payload["Label"])
            self.assertIs(True, payload["RunAtLoad"])
            self.assertEqual(1800, payload["StartInterval"])
            self.assertEqual(str(resolved_root), payload["WorkingDirectory"])
            self.assertEqual(
                "en", payload["EnvironmentVariables"]["AARON_READER_LANG"]
            )
            self.assertNotIn("PYTHONPATH", payload["EnvironmentVariables"])
            self.assertEqual(
                [
                    str(resolved_root / "aaron-reader"),
                    "--config",
                    str(resolved_root / "config" / "sources.json"),
                    "sync",
                    "--notify",
                ],
                payload["ProgramArguments"],
            )
            self.assertEqual(
                str(resolved_root / "data" / "launchd.log"),
                payload["StandardOutPath"],
            )
            self.assertEqual(
                str(resolved_root / "data" / "launchd.error.log"),
                payload["StandardErrorPath"],
            )

    def test_renderer_rejects_invalid_interval_and_incomplete_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "job.plist"
            invalid_interval = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "render_launchd.py"),
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                    "--interval",
                    "299",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment_with_language(None),
            )
            self.assertNotEqual(0, invalid_interval.returncode)
            self.assertIn("between 300 and 604800", invalid_interval.stderr)

            incomplete = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "render_launchd.py"),
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                    "--interval",
                    "3600",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment_with_language(None),
            )
            self.assertNotEqual(0, incomplete.returncode)
            self.assertIn("reader root is incomplete", incomplete.stderr)

            localized = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "render_launchd.py"),
                    "--root",
                    str(root),
                    "--output",
                    str(output),
                    "--interval",
                    "299",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment_with_language("zh-CN"),
            )
            self.assertNotEqual(0, localized.returncode)
            self.assertIn("间隔必须在 300 到 604800 秒之间", localized.stderr)


class LaunchdLifecycleScriptTests(unittest.TestCase):
    def test_shell_scripts_are_syntactically_valid_and_executable(self) -> None:
        for name in (
            "install-launchd.sh",
            "status-launchd.sh",
            "uninstall-launchd.sh",
        ):
            with self.subTest(name=name):
                path = SCRIPTS / name
                result = subprocess.run(
                    ["/bin/sh", "-n", str(path)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(path.stat().st_mode & 0o100)

    def test_install_preflights_lints_atomically_replaces_and_does_not_kickstart(self) -> None:
        script = (SCRIPTS / "install-launchd.sh").read_text(encoding="utf-8")
        self.assertIn("LAUNCHCTL=/bin/launchctl", script)
        self.assertIn("ID=/usr/bin/id", script)
        self.assertIn("PLUTIL=/usr/bin/plutil", script)
        self.assertIn('"$READER_ROOT/aaron-reader" doctor', script)
        self.assertIn("install_runtime.py", script)
        self.assertIn("Library/Application Support/Aaron Reader", script)
        self.assertIn('"$RUNTIME_ROOT/aaron-reader" doctor', script)
        self.assertIn('$PLUTIL -lint "$TEMP_PLIST"', script)
        self.assertIn('/bin/mv -f "$TEMP_PLIST" "$PLIST_PATH"', script)
        self.assertIn('bootstrap "$DOMAIN" "$PLIST_PATH"', script)
        self.assertIn("restoring the previous plist", script)
        self.assertIn("SUDO_UID", script)
        self.assertNotIn("kickstart", script)

    def test_status_is_strict_and_all_lifecycle_scripts_reject_sudo(self) -> None:
        status = (SCRIPTS / "status-launchd.sh").read_text(encoding="utf-8")
        self.assertIn('status --strict', status)
        self.assertIn("PLUTIL=/usr/bin/plutil", status)
        self.assertIn("WorkingDirectory", status)
        self.assertIn("last exit code", status)
        self.assertIn("launchd.error.log", status)
        self.assertIn(".source-root", status)
        for name in (
            "install-launchd.sh",
            "status-launchd.sh",
            "uninstall-launchd.sh",
        ):
            script = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertIn("ID=/usr/bin/id", script)
            self.assertIn("LAUNCHCTL=/bin/launchctl", script)
            self.assertIn("SUDO_USER", script)
            self.assertIn("UI_LANG=${AARON_READER_LANG:-en}", script)

    def test_lifecycle_messages_default_to_english_and_support_chinese(self) -> None:
        arguments = {
            "install-launchd.sh": ["one", "two"],
            "status-launchd.sh": ["unexpected"],
            "uninstall-launchd.sh": ["unexpected"],
        }
        for name, extra_arguments in arguments.items():
            with self.subTest(name=name, language="default"):
                result = subprocess.run(
                    ["/bin/sh", str(SCRIPTS / name), *extra_arguments],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment_with_language(None),
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("Usage:", result.stderr)
                self.assertNotIn("用法：", result.stderr)

            with self.subTest(name=name, language="zh-CN"):
                result = subprocess.run(
                    ["/bin/sh", str(SCRIPTS / name), *extra_arguments],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment_with_language("zh-CN"),
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("用法：", result.stderr)

    def test_lifecycle_scripts_preserve_no_sudo_guard_in_both_languages(self) -> None:
        for name in (
            "install-launchd.sh",
            "status-launchd.sh",
            "uninstall-launchd.sh",
        ):
            with self.subTest(name=name, language="default"):
                environment = environment_with_language(None)
                environment["SUDO_USER"] = "not-allowed"
                result = subprocess.run(
                    ["/bin/sh", str(SCRIPTS / name)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("do not use sudo or root", result.stderr)

            with self.subTest(name=name, language="zh-CN"):
                environment = environment_with_language("zh-CN")
                environment["SUDO_USER"] = "not-allowed"
                result = subprocess.run(
                    ["/bin/sh", str(SCRIPTS / name)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("不要使用 sudo 或 root", result.stderr)

    def test_readmes_are_complete_linked_equivalents(self) -> None:
        english = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (REPOSITORY_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        for heading in (
            "## Quick start",
            "## Scheduled syncing",
            "## Common commands",
            "## Minimal input packets for an LLM",
            "## Outputs and data",
            "## Fetch strategy",
            "## Configuration and extension",
            "## Design boundaries",
        ):
            self.assertIn(heading, english)
        for heading in (
            "## 立即使用",
            "## 定时同步",
            "## 常用命令",
            "## 给 LLM 的最小输入包",
            "## 输出与数据",
            "## 抓取策略",
            "## 配置与扩展",
            "## 设计边界",
        ):
            self.assertIn(heading, chinese)

        self.assertIn("Do not use `sudo` or root", english)
        self.assertIn("move or rename the project directory", english)
        self.assertIn("does not rotate these files", english)
        self.assertIn("AARON_READER_LANG=zh-CN", english)
        self.assertIn("AARON_READER_LANG=en", english)
        self.assertIn("不要使用 `sudo` 或 root", chinese)
        self.assertIn("移动或重命名项目目录", chinese)
        self.assertIn("不会替本项目轮转", chinese)
        self.assertIn("AARON_READER_LANG=zh-CN", chinese)
        self.assertIn("AARON_READER_LANG=en", chinese)


if __name__ == "__main__":
    unittest.main()
