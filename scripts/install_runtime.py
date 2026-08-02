#!/usr/bin/python3
"""Install the background-safe Aaron Reader runtime without copying secrets.

macOS background LaunchAgents may be denied access to Documents, Desktop, or
Downloads even when an interactive terminal can read those folders.  The
installer therefore keeps scheduled code and state in the current user's
Application Support directory.  Program/config files are refreshed on every
install; an existing installed database is deliberately preserved.
"""

import argparse
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def atomic_copy(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % destination.name,
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(str(source), str(temporary))
        temporary.chmod(0o755 if executable else 0o600)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s." % destination.name,
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(str(source)) as source_connection:
            with sqlite3.connect(str(temporary)) as destination_connection:
                source_connection.backup(destination_connection)
        temporary.chmod(0o600)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install(source_root: Path, runtime_root: Path) -> None:
    if runtime_root.is_symlink():
        raise ValueError("runtime root must not be a symbolic link")
    source_root = source_root.resolve()
    runtime_root = runtime_root.resolve()
    if (
        source_root == runtime_root
        or source_root in runtime_root.parents
        or runtime_root in source_root.parents
    ):
        raise ValueError("runtime root must be separate from the source checkout")

    entrypoint = source_root / "aaron-reader"
    config = source_root / "config" / "sources.json"
    package = source_root / "src" / "aaron_reader"
    required = (entrypoint, config, package / "__main__.py")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("source checkout is incomplete: %s" % ", ".join(missing))

    runtime_root.mkdir(parents=True, exist_ok=True)
    for directory in (
        runtime_root / "data",
        runtime_root / "public",
        runtime_root / "config",
        runtime_root / "src",
    ):
        if directory.is_symlink():
            raise ValueError("runtime directories must not be symbolic links")
        directory.mkdir(exist_ok=True)
    atomic_copy(entrypoint, runtime_root / "aaron-reader", executable=True)
    atomic_copy(config, runtime_root / "config" / "sources.json")
    for source in sorted(package.glob("*.py")):
        atomic_copy(source, runtime_root / "src" / "aaron_reader" / source.name)

    marker = runtime_root / ".source-root"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".source-root.", dir=str(runtime_root)
    )
    os.close(descriptor)
    temporary_marker = Path(temporary_name)
    try:
        temporary_marker.write_text(str(source_root) + "\n", encoding="utf-8")
        temporary_marker.chmod(0o600)
        os.replace(str(temporary_marker), str(marker))
    finally:
        try:
            temporary_marker.unlink()
        except FileNotFoundError:
            pass

    installed_database = runtime_root / "data" / "reader.sqlite3"
    source_database = source_root / "data" / "reader.sqlite3"
    if installed_database.exists() and (
        installed_database.is_symlink() or not installed_database.is_file()
    ):
        raise ValueError("installed database must be a regular file")
    if not installed_database.exists() and source_database.is_file():
        backup_database(source_database, installed_database)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args()
    install(Path(args.source_root), Path(args.runtime_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
