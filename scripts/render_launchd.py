#!/usr/bin/python3
import argparse
import os
import plistlib
from pathlib import Path


def localized(english: str, simplified_chinese: str) -> str:
    if os.environ.get("AARON_READER_LANG") == "zh-CN":
        return simplified_chinese
    return english


def main() -> int:
    parser = argparse.ArgumentParser(
        description=localized(
            "Render the per-user Aaron Reader LaunchAgent plist.",
            "生成当前用户的 Aaron Reader LaunchAgent plist。",
        )
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=int, required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output = Path(args.output).expanduser().resolve()
    if args.interval < 300 or args.interval > 604800:
        raise SystemExit(
            localized(
                "interval must be between 300 and 604800 seconds",
                "间隔必须在 300 到 604800 秒之间",
            )
        )
    if not root.is_dir():
        raise SystemExit(
            localized(
                "reader root is not a directory: %s" % root,
                "项目根目录不是目录：%s" % root,
            )
        )
    required = (
        root / "aaron-reader",
        root / "config" / "sources.json",
        root / "src" / "aaron_reader" / "__main__.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(
            localized(
                "reader root is incomplete; missing: %s" % ", ".join(missing),
                "项目根目录不完整；缺少：%s" % ", ".join(missing),
            )
        )
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "Label": "com.aaron.reader",
        "ProgramArguments": [
            str(root / "aaron-reader"),
            "--config",
            str(root / "config" / "sources.json"),
            "sync",
            "--notify",
        ],
        "EnvironmentVariables": {
            "AARON_READER_LANG": "en",
            "PYTHONUNBUFFERED": "1",
        },
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StartInterval": args.interval,
        "ProcessType": "Background",
        "StandardOutPath": str(data_dir / "launchd.log"),
        "StandardErrorPath": str(data_dir / "launchd.error.log"),
    }
    with output.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
