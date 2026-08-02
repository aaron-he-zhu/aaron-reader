#!/usr/bin/env python3
"""Prepare a validated public snapshot and optionally push it to GitHub.

Cloudflare Workers Builds deploys the pushed commit. This fixed program never
uses a model, a model API key, a Cloudflare token, or a hosting-specific archive.
By default it refreshes the deterministic feeds first. ``--skip-sync`` is the
GitHub Actions post-enrichment path: it renders and publishes the populated
ephemeral database without performing another network crawl.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
PUBLIC = ROOT / "public"
LOCK = ROOT / "data" / "cloudflare-release.lock"

SNAPSHOT_PATHS = (
    "site/data/latest.json",
    "site/public/reader/latest.json",
    "site/public/reader/feed.xml",
    "site/public/reader/digest.md",
)
STATE_PATHS = (
    "crawler/latest.json",
    "cloud/ai-cache.json",
)
PUBLICATION_PATHS = STATE_PATHS + SNAPSHOT_PATHS
PUBLICATION_SIZE_LIMITS = {
    "crawler/latest.json": 25 * 1024 * 1024,
    "cloud/ai-cache.json": 25 * 1024 * 1024,
    "site/data/latest.json": 25 * 1024 * 1024,
    "site/public/reader/latest.json": 25 * 1024 * 1024,
    "site/public/reader/feed.xml": 25 * 1024 * 1024,
    "site/public/reader/digest.md": 5 * 1024 * 1024,
}
SIZE_LIMITS = {
    "latest.json": 25 * 1024 * 1024,
    "feed.xml": 25 * 1024 * 1024,
    "digest.md": 5 * 1024 * 1024,
}
PUBLIC_DIGEST_ARTICLE_LIMIT = 100
_MARKDOWN_PUNCTUATION = re.compile(
    r"([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])"
)


class ReleaseError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    capture: bool = False,
) -> str:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ReleaseError(
            "command failed (%s): %s%s"
            % (
                completed.returncode,
                " ".join(command),
                ("\n" + detail) if detail else "",
            )
        )
    # Git porcelain status uses its leading two columns as state.
    return (completed.stdout or "").rstrip()


def _validate_regular_file(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("required output is not a regular file: %s" % path)
    size = path.stat().st_size
    if size <= 0 or size > maximum:
        raise ReleaseError("output size is outside the safe range: %s" % path)
    return path.read_bytes()


def _validate_report_projection(
    snapshot: Mapping[str, object], cache: Mapping[str, object]
) -> None:
    cache_reports = cache.get("reports")
    if not isinstance(cache_reports, list):
        raise ReleaseError("cloud AI cache is missing its report array")
    snapshot_reports = snapshot.get("ai_reports")
    if not isinstance(snapshot_reports, list) or snapshot.get(
        "cached_ai_report_count"
    ) != len(snapshot_reports):
        raise ReleaseError("latest.json AI report count is inconsistent")
    if len(snapshot_reports) != len(cache_reports):
        raise ReleaseError(
            "rendered AI reports are not reproducible from the public AI cache"
        )
    identity_fields = (
        "period",
        "target_language",
        "timezone",
        "local_date",
        "period_start",
        "period_end",
    )
    snapshot_keys = {
        tuple(str(report.get(field) or "") for field in identity_fields)
        for report in snapshot_reports
        if isinstance(report, dict)
    }
    cache_keys = {
        tuple(str(report.get(field) or "") for field in identity_fields)
        for report in cache_reports
        if isinstance(report, dict)
    }
    if (
        len(snapshot_keys) != len(snapshot_reports)
        or len(cache_keys) != len(cache_reports)
        or snapshot_keys != cache_keys
    ):
        raise ReleaseError(
            "rendered AI report identities differ from the public AI cache"
        )


def _validate_outputs() -> dict[str, object]:
    json_bytes = _validate_regular_file(PUBLIC / "latest.json", SIZE_LIMITS["latest.json"])
    feed_bytes = _validate_regular_file(PUBLIC / "feed.xml", SIZE_LIMITS["feed.xml"])
    digest_bytes = _validate_regular_file(PUBLIC / "digest.md", SIZE_LIMITS["digest.md"])
    cache_bytes = _validate_regular_file(
        ROOT / "cloud" / "ai-cache.json",
        PUBLICATION_SIZE_LIMITS["cloud/ai-cache.json"],
    )

    try:
        snapshot = json.loads(json_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("latest.json is invalid: %s" % exc) from exc
    if not isinstance(snapshot, dict):
        raise ReleaseError("latest.json must contain an object")
    try:
        cache = json.loads(cache_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("cloud AI cache is invalid: %s" % exc) from exc
    if not isinstance(cache, dict):
        raise ReleaseError("cloud AI cache must contain an object")
    if not isinstance(snapshot.get("articles"), list) or not isinstance(
        snapshot.get("sources"), list
    ):
        raise ReleaseError("latest.json is missing article or source arrays")
    _validate_report_projection(snapshot, cache)
    if snapshot.get("render_llm_tokens_used") != 0:
        raise ReleaseError("static rendering unexpectedly reported LLM token use")
    if snapshot.get("llm_tokens_used") != 0:
        raise ReleaseError("deterministic sync unexpectedly reported LLM token use")
    if len(snapshot["articles"]) > 10000 or len(snapshot["sources"]) > 100:
        raise ReleaseError("snapshot cardinality exceeds the publication limit")
    try:
        ET.fromstring(feed_bytes)
    except ET.ParseError as exc:
        raise ReleaseError("feed.xml is invalid: %s" % exc) from exc
    if not digest_bytes.strip():
        raise ReleaseError("digest.md is empty")
    return snapshot


def _public_snapshot(snapshot: Mapping[str, object]) -> dict[str, object]:
    public = copy.deepcopy(dict(snapshot))

    counts = public.get("counts")
    if isinstance(counts, dict):
        counts.pop("unread", None)
        counts.pop("starred", None)

    sources = public.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            source.pop("unread_count", None)
            source.pop("pending_count", None)
            source.pop("last_error", None)

    articles = public.get("articles")
    if isinstance(articles, list):
        for article in articles:
            if not isinstance(article, dict):
                continue
            article.pop("unread", None)
            article.pop("starred", None)
            artifacts = article.get("ai_artifacts")
            if isinstance(artifacts, list):
                for artifact in artifacts:
                    if not isinstance(artifact, dict):
                        continue
                    artifact.pop("provider", None)
                    artifact.pop("model", None)

    reports = public.get("ai_reports")
    if isinstance(reports, list):
        for report in reports:
            if not isinstance(report, dict):
                continue
            report.pop("provider", None)
            report.pop("model", None)

    return public


def _plain_text(value: object) -> str:
    """Return one inert line of display text for a public Markdown artifact."""

    text = "" if value is None else str(value)
    text = "".join(character for character in text if ord(character) >= 32)
    return re.sub(r"\s+", " ", text).strip()


def _escape_markdown(value: object) -> str:
    return _MARKDOWN_PUNCTUATION.sub(
        lambda match: "\\" + match.group(1),
        _plain_text(value),
    )


def _safe_http_url(value: object) -> str:
    raw = _plain_text(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
    except (TypeError, ValueError):
        return ""
    return quote(raw, safe=":/?#[]@!$&'()*+,;=%")


def _public_digest(snapshot: Mapping[str, object]) -> str:
    """Render a public digest without copying ephemeral read-state fields."""

    raw_articles = snapshot.get("articles")
    articles = (
        [article for article in raw_articles if isinstance(article, dict)]
        if isinstance(raw_articles, list)
        else []
    )
    selected = articles[:PUBLIC_DIGEST_ARTICLE_LIMIT]
    lines = [
        "# Aaron Reader Public Digest",
        "",
        "Generated by a fixed program; LLM tokens: 0.",
        "",
    ]
    if not selected:
        lines.append("There are no public articles.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "Showing the latest %d of %d public articles (limit: %d)."
            % (len(selected), len(articles), PUBLIC_DIGEST_ARTICLE_LIMIT),
            "",
        ]
    )
    for article in selected:
        title = _escape_markdown(article.get("title") or "Untitled")
        url = _safe_http_url(article.get("url"))
        title_markup = "[%s](<%s>)" % (title, url) if url else title
        source_name = _escape_markdown(article.get("source_name") or "Unknown source")
        published_at = _plain_text(article.get("published_at"))
        date = _escape_markdown(published_at[:10])
        lines.append("- %s — %s · %s" % (title_markup, source_name, date))
    return "\n".join(lines) + "\n"


def _atomic_write(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    temporary.write_bytes(payload)
    os.replace(str(temporary), str(destination))


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(".%s.tmp" % destination.name)
    shutil.copyfile(str(source), str(temporary))
    os.replace(str(temporary), str(destination))


def _publication_hashes() -> dict[str, str]:
    """Hash every commit-eligible file so build code cannot alter it later."""

    result: dict[str, str] = {}
    for relative in PUBLICATION_PATHS:
        payload = _validate_regular_file(
            ROOT / relative,
            PUBLICATION_SIZE_LIMITS[relative],
        )
        result[relative] = hashlib.sha256(payload).hexdigest()
    return result


def _copy_snapshot(snapshot: Mapping[str, object]) -> None:
    public_snapshot = _public_snapshot(snapshot)
    payload = (json.dumps(public_snapshot, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    _atomic_write(SITE / "data" / "latest.json", payload)
    _atomic_write(SITE / "public" / "reader" / "latest.json", payload)
    _atomic_copy(PUBLIC / "feed.xml", SITE / "public" / "reader" / "feed.xml")
    _atomic_write(
        SITE / "public" / "reader" / "digest.md",
        _public_digest(public_snapshot).encode("utf-8"),
    )


def _unexpected_changes(lines: Iterable[str]) -> list[str]:
    allowed = set(PUBLICATION_PATHS)
    unexpected: list[str] = []
    for line in lines:
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path not in allowed:
            unexpected.append(line)
    return unexpected


def _ensure_clean_source() -> None:
    status = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture=True,
    )
    unexpected = _unexpected_changes(status.splitlines())
    if unexpected:
        raise ReleaseError(
            "source has unrelated changes; refusing unattended release:\n%s"
            % "\n".join(unexpected)
        )


def _verify_build() -> None:
    required = (
        SITE / "dist" / "server" / "index.js",
        SITE / "dist" / "server" / "wrangler.json",
        SITE / "dist" / "client" / "reader" / "latest.json",
        SITE / "dist" / "client" / "reader" / "feed.xml",
        SITE / "dist" / "client" / "reader" / "digest.md",
        SITE / ".wrangler" / "deploy" / "config.json",
    )
    for path in required:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            raise ReleaseError("build output is missing or invalid: %s" % path)
    if any(path.name == ".openai" for path in (SITE / "dist").rglob(".openai")):
        raise ReleaseError("the Cloudflare build unexpectedly contains Sites metadata")

    try:
        config = json.loads((SITE / "dist" / "server" / "wrangler.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("generated Wrangler configuration is invalid: %s" % exc) from exc
    assets = config.get("assets")
    if config.get("name") != "aaron-reader" or config.get("main") != "index.js":
        raise ReleaseError("generated Worker identity or entry point is incorrect")
    if "nodejs_compat" not in config.get("compatibility_flags", []):
        raise ReleaseError("generated Worker is missing nodejs_compat")
    if not isinstance(assets, dict) or assets.get("binding") != "ASSETS":
        raise ReleaseError("generated Worker is missing its static asset binding")
    if "/reader/feed.xml" not in assets.get("run_worker_first", []):
        raise ReleaseError("the RSS rewrite route does not run through the Worker")
    for forbidden in ("d1_databases", "r2_buckets", "kv_namespaces", "ai", "images"):
        if config.get(forbidden):
            raise ReleaseError("unexpected Cloudflare binding in build: %s" % forbidden)


def _commit_snapshot(snapshot: Mapping[str, object]) -> str:
    _run(["git", "add", "--", *PUBLICATION_PATHS])
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", *PUBLICATION_PATHS],
        cwd=str(ROOT),
        check=False,
    )
    if staged.returncode == 0:
        return ""
    if staged.returncode != 1:
        raise ReleaseError("could not inspect the staged public snapshot")
    generated_at = str(snapshot.get("generated_at") or "unknown")
    _run([
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        "Update reader cloud state %s" % generated_at,
    ])
    return _run(["git", "rev-parse", "HEAD"], capture=True)


def _github_repo_slug(remote_url: str) -> str:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote_url.strip(),
    )
    if not match:
        raise ReleaseError("origin must be an explicit github.com repository")
    return match.group(1)


def _push_public_main() -> str:
    branch = _run(["git", "branch", "--show-current"], capture=True)
    if branch != "main":
        raise ReleaseError("unattended publication is allowed only from main")
    remote_url = _run(["git", "remote", "get-url", "origin"], capture=True)
    repository = _github_repo_slug(remote_url)
    try:
        repository_info = json.loads(
            _run(
                ["gh", "repo", "view", repository, "--json", "isPrivate"],
                capture=True,
            )
        )
    except (json.JSONDecodeError, ReleaseError) as exc:
        raise ReleaseError("could not verify the GitHub repository visibility") from exc
    if repository_info.get("isPrivate") is not False:
        raise ReleaseError("the publication repository is not public")
    _run(["git", "push", "origin", "HEAD:main"])
    return repository


def _reader_command(database_path: Optional[Path], *arguments: str) -> list[str]:
    command = [str(ROOT / "aaron-reader")]
    if database_path is not None:
        command.extend(["--database", str(database_path)])
    command.extend(arguments)
    return command


def _validated_database_path(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    candidate = Path(value)
    unresolved = candidate if candidate.is_absolute() else ROOT / candidate
    if unresolved.is_symlink():
        raise ReleaseError("release database must not be a symbolic link")
    resolved = unresolved.resolve()
    data_root = (ROOT / "data").resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ReleaseError("release database must be inside the repository data directory") from exc
    if not resolved.is_file():
        raise ReleaseError("release database is not a regular file: %s" % resolved)
    return resolved


def prepare(
    *,
    push: bool = False,
    skip_sync: bool = False,
    database_path: Optional[Path] = None,
) -> Mapping[str, object]:
    _ensure_clean_source()
    starting_head = _run(["git", "rev-parse", "HEAD"], capture=True)
    if skip_sync:
        # Render only from the selected already-validated state. In production
        # this is a fresh database reconstructed from both public handoffs, so
        # the committed site must be reproducible on another disposable runner.
        _run(_reader_command(database_path, "render"))
    else:
        if database_path is not None:
            raise ReleaseError("--database requires --skip-sync")
        _run(_reader_command(None, "sync"))
    _run(_reader_command(database_path, "status", "--strict"))
    snapshot = _validate_outputs()
    _copy_snapshot(snapshot)
    _run(["npm", "run", "validate:data"], cwd=SITE)
    expected_publication_hashes = _publication_hashes()
    _run(["npm", "run", "lint"], cwd=SITE)
    _run(["npm", "run", "typecheck"], cwd=SITE)
    _run(["npm", "run", "build"], cwd=SITE)
    _run(["node", "--test", "tests/rendered-html.test.mjs"], cwd=SITE)
    _verify_build()
    if _run(["git", "rev-parse", "HEAD"], capture=True) != starting_head:
        raise ReleaseError("a build or test changed the checked-out Git commit")
    _ensure_clean_source()
    if _publication_hashes() != expected_publication_hashes:
        raise ReleaseError(
            "a build or test changed a commit-eligible public state file"
        )
    commit_sha = _commit_snapshot(snapshot)
    head_sha = commit_sha or _run(["git", "rev-parse", "HEAD"], capture=True)

    repository = _push_public_main() if push else None
    return {
        "status": "ready" if commit_sha else "unchanged",
        "commit_sha": head_sha,
        "pushed": push,
        "sync_skipped": skip_sync,
        "repository": repository,
        "article_count": len(snapshot["articles"]),
        "generated_at": snapshot.get("generated_at"),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push the validated main commit to the verified public GitHub origin",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help=(
            "Render and publish the populated database without fetching feeds"
        ),
    )
    parser.add_argument(
        "--database",
        help=(
            "With --skip-sync, render from this validated SQLite file under "
            "the repository data directory"
        ),
    )
    args = parser.parse_args(argv)
    database_path = _validated_database_path(args.database)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        result = prepare(
            push=args.push,
            skip_sync=args.skip_sync,
            database_path=database_path,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
