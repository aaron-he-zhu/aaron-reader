# Aaron Reader

[简体中文](README.zh-CN.md)

[Live Cloudflare site](https://aaron-reader.aaron-he-zhu.workers.dev/) ·
[GitHub repository](https://github.com/aaron-he-zhu/aaron-reader)

A local, deterministic blog reader that uses **zero LLM tokens by default**. It currently subscribes to:

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

Normal syncs never call OpenAI, Anthropic, or any other model API, and no API key is required. Fetching, conditional caching, page parsing, URL canonicalization, content fingerprinting, deduplication, SQLite persistence, unread state, notifications, and Markdown digests are all handled by deterministic code.

## Quick start

The project has no third-party runtime dependencies. The Python 3.9 included with macOS is sufficient:

```bash
./aaron-reader sync
./aaron-reader status
./aaron-reader list --unread
./aaron-reader serve --open
```

English is the canonical interface and the default. Simplified Chinese is
available throughout the CLI, diagnostics, generated HTML/JSON/RSS/digest
artifacts, and the local reading page. Select it for one command with either
form below:

```bash
./aaron-reader --language zh-CN status
./aaron-reader status --language zh-CN
```

To keep using Chinese for the current shell, set the environment variable:

```bash
AARON_READER_LANG=zh-CN ./aaron-reader status
```

Language precedence is `--language`, `AARON_READER_LANG`, the
`default_language` value in `config/sources.json`, and finally English. The
reading page also has a visible English / 简体中文 selector and remembers the
browser choice. These controls translate the reader interface only: article
titles and publisher descriptions remain exactly as published, so language
support does not add background translation, model calls, or LLM tokens.

Use the `./aaron-reader` wrapper in the repository root. Configuration and runtime data are also resolved relative to this source tree, so a global command created by `pip install` is not a supported way to run the project.

The first `sync` establishes a historical baseline. Baseline articles are stored but automatically marked as read, and they do not generate a flood of old-article notifications. Only genuinely new articles found by later syncs remain unread and trigger macOS notifications.

The reading page is generated at `public/index.html`. `serve --open` starts a read-only local server on `127.0.0.1:8765`; it is not exposed to the LAN. The server can read only five generated artifacts, does not list directories, and does not follow symbolic links. To allow LAN access, you must explicitly use `--allow-network`, for example `serve --host 0.0.0.0 --allow-network`. That mode has no authentication, so use it only on a trusted network. Optional AI buttons are a separate, explicit loopback-only mode described below and cannot be combined with `--allow-network`.

## Scheduled syncing

This checkout is currently scheduled through the ChatGPT/Codex desktop app,
not launchd. The active project task is **Aaron Reader twice-daily sync**: it
runs at 10:00 and 22:00 in `America/Los_Angeles` with `gpt-5.6-luna` and
`medium` reasoning. The model generates one combined Chinese
summary/translation result for at most three new unread articles. Fixed
programs perform feed fetching, pending-input selection, strict result and
fingerprint validation, atomic caching, health checks, public-state redaction,
frontend lint/type/build/test, and the exact snapshot commit. The task then
pushes that commit to the public GitHub repository; Cloudflare Workers Builds
builds and deploys the pushed source. Ordinary syncing and publishing do not
call the OpenAI API, and no model API key or Cloudflare credential is stored in
the repository.

The computer must stay on and the ChatGPT desktop app must be running when a
scheduled task needs this local project. A failed GitHub push or Cloudflare
build leaves the previous successful Worker deployment live.

The LaunchAgent workflow below remains an optional local-only fallback. Do not
enable it at the same time as the Codex task, because both schedulers would
write the reader database and generated outputs. It is disabled in the current
installation; uninstalling it preserved the old installed runtime and database.

On macOS, install a per-user LaunchAgent that runs once per hour. **Do not use `sudo` or root.** Notifications, `~/Library/LaunchAgents`, and the GUI launchd domain all belong to the currently logged-in user:

```bash
./scripts/install-launchd.sh
```

The installer first runs the offline `doctor` check. It then installs a background-safe copy of the dependency-free program and configuration under `~/Library/Application Support/Aaron Reader`, along with a consistent SQLite backup on the first install. This avoids asking macOS for Full Disk Access when the source checkout is under Documents, Desktop, or Downloads. Reinstalling refreshes program/config files while preserving the installed database. The installer renders a temporary plist under `~/Library/LaunchAgents`, validates it with the system `plutil`, and atomically replaces the installed plist. During a reinstall, the previous plist is backed up first; if the new job cannot be loaded, the installer makes a best-effort rollback to the previous job. The plist uses `RunAtLoad`, so launchd starts the first sync after a successful load without an additional forced kill and restart.

You can provide an interval in seconds. For example, to sync every 30 minutes:

```bash
./scripts/install-launchd.sh 1800
```

Inspect or uninstall the job with:

```bash
./scripts/status-launchd.sh
./scripts/uninstall-launchd.sh
```

The scheduled reader's current page and command live in Application Support:

```bash
open "$HOME/Library/Application Support/Aaron Reader/public/index.html"
"$HOME/Library/Application Support/Aaron Reader/aaron-reader" status
```

After changing source code or `config/sources.json` in this checkout, rerun `install-launchd.sh` to refresh the installed runtime. The scheduled database, read/star state, cached outputs, and logs remain in Application Support and are not overwritten by a reinstall.

Lifecycle scripts print English messages by default. To request Simplified Chinese messages, set `AARON_READER_LANG=zh-CN`, for example:

```bash
AARON_READER_LANG=zh-CN ./scripts/status-launchd.sh
```

The generated LaunchAgent explicitly sets `AARON_READER_LANG=en`, keeping unattended log output in English even if the installer itself was run with Chinese messages.

`status-launchd.sh` checks the plist, launchd registration, the last scheduled exit code, whether the installed runtime belongs to this checkout, and strict source health; it returns a nonzero status if any check fails. The runtime records the absolute path of its source checkout. If you move or rename the project directory, run `install-launchd.sh` again from its new location.

Uninstalling moves the plist to the Trash. It does not delete the installed runtime, database, articles, generated static files, or logs. Standard output is written to `~/Library/Application Support/Aaron Reader/data/launchd.log`, while errors and warnings are written to the adjacent `launchd.error.log`. launchd does not rotate these files for this project; inspect, archive, or remove old logs periodically during long-term use.

### GitHub and Cloudflare release boundary

The public production deployment is
[aaron-reader.aaron-he-zhu.workers.dev](https://aaron-reader.aaron-he-zhu.workers.dev/).
Cloudflare Workers Builds tracks the GitHub `main` branch, builds from `/site`,
and deploys only after a successful GitHub push. Non-production branch builds
are disabled.

`site/` is the vinext/Cloudflare Worker surface inside this repository. It
contains the read-only bilingual interface and an explicit public projection of
the JSON, RSS, and Markdown snapshots. The projection removes private read,
star, pending, and raw error state before Git history or Cloudflare sees it. It
contains no SQLite database, API key, D1/R2/Images binding, or hosted AI
endpoint. The hosted interface defaults to English and offers Simplified
Chinese.

To exercise the same deterministic preparation used by the scheduled task:

```bash
./scripts/prepare_cloudflare_release.py
```

The final JSON result is `unchanged`, `ready`, or `failed`. The command syncs,
validates and redacts the public snapshot, runs the complete site checks, and
creates at most one exact snapshot commit. It does not contact Cloudflare. Once
the public GitHub `origin` and Cloudflare Workers Builds are configured, the
same fixed program can push the verified `main` commit:

```bash
./scripts/prepare_cloudflare_release.py --push
```

Cloudflare builds from the committed files under `site/`; a fresh Git clone
does not need the ignored local database or `public/` directory. The
`codex://` buttons store an optional local checkout path only in the visitor's
browser, never in Git or the deployed snapshot.

## Common commands

```bash
# Sync one source
./aaron-reader sync --source claude-blog

# Ignore locally stored HTTP validators and body hashes
./aaron-reader sync --force

# Keep historical articles unread during the first import
./aaron-reader sync --keep-history-unread

# List, search, and filter starred articles
./aaron-reader list --limit 30
./aaron-reader list --unread --source anthropic-news
./aaron-reader list --query Codex
./aaron-reader list --starred

# Read state and stars
./aaron-reader read 12 13
./aaron-reader unread 12
./aaron-reader read --all
./aaron-reader star 12
./aaron-reader unstar 12

# Generate deterministic Markdown without calling an LLM
./aaron-reader digest

# Suitable for monitoring: nonzero if any source is unsynced, stale, degraded, or unhealthy
./aaron-reader status --strict

# Validate configuration and the database; --live performs online parser contract checks without storing articles
./aaron-reader doctor
./aaron-reader doctor --live
```

Available source slugs:

- `openai-news`
- `openai-developers`
- `claude-blog`
- `anthropic-news`

## Optional AI summaries and translations

Aaron Reader now includes a complete but **default-off** AI enrichment sidecar. Normal `sync`, `status`, `list`, `render`, deterministic `digest`, notifications, LaunchAgent runs, and ordinary page views still make no model request and require no API key. AI results live in separate tables and never overwrite a publisher title or description.

### ChatGPT/Codex subscription bridge (no API key)

The preferred unattended path can use the model selected for a ChatGPT/Codex
desktop task instead of making an API request from Python. The fixed program
exports only bounded publisher metadata that still needs enrichment:

```bash
./aaron-reader ai subscription-export --unread --limit 3 --to zh-CN \
  --output data/subscription-ai-request.json
```

The command atomically writes compact JSON and prints a small status object
containing `pending_count`, `request_path`, and `suggested_result_path`. A
Codex task reads the request, follows the embedded instructions and schemas,
and writes one combined summary/translation result per article to the
suggested result path. When `pending_count` is zero, no model work or import is
needed. Import the completed file with:

```bash
./aaron-reader ai subscription-import \
  data/subscription-ai-request.results.json --json
```

Web actions and one-off Codex requests can target an exact article and only
the task the user selected. Both options are repeatable; omitting `--task`
preserves the original combined summary-and-translation behavior:

```bash
# Summary only for article 110, even if it is already marked read
./aaron-reader ai subscription-export --article-id 110 --task summary \
  --to zh-CN --output data/subscription-ai-request.json

# Translation only
./aaron-reader ai subscription-export --article-id 110 --task translation \
  --to zh-CN --output data/subscription-ai-request.json
```

The result contract still has both `summary` and `translation` fields. An
unrequested field must be `null`; returning an unrequested result is rejected.
The selected task is bound into the item fingerprint, while the default
both-task fingerprint remains backward compatible.

The hosted reader keeps historical backfill explicit so the low-token default
does not silently process the baseline archive. Its **Backfill 3 articles in
Chinese** action opens a bounded Codex task equivalent to:

```bash
./aaron-reader ai subscription-export --all --limit 3 --to zh-CN \
  --output data/subscription-ai-request.json
```

The exporter skips valid cached artifacts and never selects more than three
articles for that run. It does not change the twice-daily `--unread` policy.
The hosted action is shown only while at least one current article lacks the
complete Chinese summary-and-translation pair; once coverage is complete, the
reader shows the completed ratio and removes the redundant backfill action.

Daily and weekly report buttons use a separate subscription request. `daily`
means the current `America/Los_Angeles` calendar day through export time;
`weekly` means Monday 00:00 in that timezone through export time. Daylight
saving transitions are handled by the timezone database.

```bash
./aaron-reader ai subscription-report-export --period daily --to zh-CN \
  --output data/subscription-daily-request.json
./aaron-reader ai subscription-report-import \
  data/subscription-daily-request.results.json --json

./aaron-reader ai subscription-report-export --period weekly --to zh-CN \
  --output data/subscription-weekly-request.json
./aaron-reader ai subscription-report-import \
  data/subscription-weekly-request.results.json --json
```

Reports use the existing strict digest schema, include at most 50 bounded
metadata entries, and are cached by period, article set/content, language,
model, prompt, schema, and generation settings. The importer rechecks the
covered time window and every article version in the same transaction that
stores the digest and its durable report record. Cached latest daily/weekly
records are exposed in `public/latest.json` as `ai_reports`.

The hosted interface displays only reports whose cached language exactly
matches the active English or Simplified Chinese interface. It never silently
places a Chinese report inside the English view. Report item summaries remain
part of that daily/weekly digest; they are not presented as independently
validated per-article summary or translation artifacts.

Export and import do not inspect `OPENAI_API_KEY`, do not require
`ai.enabled=true`, do not construct the API provider, and do not consume the
API-sidecar budget. The importer rejects extra fields, duplicate JSON keys,
invalid summary/translation structures, reordered batches, and any article,
input, prompt, schema, model, or generation configuration whose fingerprint
changed after export. Both artifacts are committed atomically; importing the
same valid result again is a local cache hit.

The OpenAI integration uses the Responses API, strict Structured Outputs, `store: false`, no tools, and one independent request per article. All AI features default to the cost-sensitive `gpt-5.6-luna` model with `medium` reasoning effort. AI itself remains globally off until explicitly enabled. Model defaults are configuration, not a promise that the provider will never change; review current [OpenAI model guidance](https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p6-sol.md) before changing them.

### 1. Preview with zero model calls

`preview` shows the exact bounded data object, character/byte counts, conservative token reservation, cache key, cache state, and current budget usage. It never constructs a provider client or calls a model, even while AI is disabled:

```bash
./aaron-reader ai preview 12 --task summary --to zh-CN
./aaron-reader ai preview 12 --task translation --to zh-CN --field title
./aaron-reader ai status
```

### 2. Explicitly enable model calls

Edit the existing `ai` object in `config/sources.json`:

```json
{
  "ai": {
    "enabled": true,
    "provider": "openai",
    "translation_model": "gpt-5.6-luna",
    "summary_model": "gpt-5.6-luna",
    "digest_model": "gpt-5.6-luna",
    "reasoning_effort": "medium",
    "store": false,
    "input_policy": "metadata_only",
    "features": {
      "summary": true,
      "translation": true,
      "digest": true,
      "full_text": false,
      "web_actions": false
    }
  }
}
```

Supply the API key only in the process environment or a shell-local secret manager integration. Do not add it to JSON, SQLite, a plist, HTML, or a command argument:

```bash
export OPENAI_API_KEY='...'
```

The program accepts only the fixed `OPENAI_API_KEY` environment name and fixed `https://api.openai.com/v1/responses` endpoint. Errors and audit records never store request bodies, authorization headers, the key, or full provider error bodies.

### 3. Generate on demand

```bash
# Directly generate a Chinese summary; this is one call, not summarize-then-translate
./aaron-reader ai summarize 12 --to zh-CN

# Translate only publisher metadata; originals remain visible and unchanged
./aaron-reader ai translate 12 --to zh-CN --field title --field publisher-summary

# Summarize a bounded unread set in one structured digest
./aaron-reader ai digest --unread --limit 20 --to zh-CN
```

The first successful result is written to `ai_artifacts`. Repeating the same task with the same exact normalized input, language, model, prompt, schema, generation parameters, and extractor version is a local cache hit and makes no provider request. Changing read/star state does not invalidate the cache; changing publisher content, target language, model, prompt, schema, requested fields, or extracted text does.

### 4. Optional full-text summaries

Full text is a separate deterministic acquisition step. It is never fetched during `sync`, never sent as raw HTML, and never used unless both the feature and an on-demand policy are enabled:

```json
{
  "ai": {
    "input_policy": "fetch_on_demand_cached_local",
    "features": {
      "summary": true,
      "translation": true,
      "digest": true,
      "full_text": true,
      "web_actions": false
    }
  }
}
```

Then use:

```bash
./aaron-reader ai fetch 12
./aaron-reader ai summarize 12 --full-text --to zh-CN
```

`fetch_on_demand_ephemeral` avoids writing extracted text to SQLite; it is available only to an immediate CLI or local-web request, cannot be batch queued, and an explicit retry fetches the text again. `fetch_on_demand_cached_local` stores a reusable local snapshot. A cached snapshot is reused only when its canonical article URL and extractor version still match. The fetcher accepts only the article's exact configured publisher host, validates every DNS answer and redirect hop, rejects localhost/private/link-local/reserved addresses and credentials, sends no cookies or authorization, accepts only bounded HTML, strips scripts/navigation/forms/ads, normalizes prose, and records hashes, extractor version, final URL, and truncation. Article text is treated as untrusted data and cannot give the model tools or instructions.

Full-article translation is intentionally not implemented: it spends far more tokens and raises copyright and partial-failure problems. Translate the title/publisher description, or summarize the extracted article directly in the desired language.

### 5. Hard budgets, queue, and audit

Every potentially billable attempt has its own durable `ai_attempts` audit record. Before network I/O, SQLite atomically reserves a UTF-8-byte upper bound plus a protocol margin for input, then adds the configured maximum output and checks daily/monthly request and token caps. Two concurrent processes cannot both spend the same remaining budget. A budget of zero blocks calls. Monetary caps are optional and work only when every selected model has an explicit, reviewed price snapshot under `ai.prices`; each snapshot must contain finite, non-negative input, output, cached-input, and cache-write-input rates. Use conservative maximum effective rates for the configured input limits, including any applicable long-context or service-tier multiplier—the program can validate the numbers' shape, not external pricing accuracy. Aaron Reader does not bake mutable prices into code.

Batching means bounded, independently cached article jobs—not many articles hidden inside one prompt. The worker is intentionally serial (`concurrency` must remain `1`) so enabling a queue cannot create an unexpected burst of simultaneous model calls:

```bash
# Also set ai.batch.enabled=true first
./aaron-reader ai batch --unread --limit 10 --task summary --to zh-CN --yes
./aaron-reader ai worker --limit 10

./aaron-reader ai status
./aaron-reader ai audit --limit 100
```

Aaron Reader never automatically replays an uncertain Responses POST. HTTP 429 becomes `permanent_failed` and can be retried explicitly with a fresh audited request. HTTP 408/409/425, 5xx, timeouts, and disconnects may have reached the provider, so they become `unknown`, keep their conservative budget reservation, and require an additional risk acknowledgement before a fresh request. Locally invalid structured output has a short, bounded retry only when exact usage was reported and the input is reproducible. Known incomplete/refusal/no-output responses with complete usage are charged to the audit using that usage; if usage or GPT-5.6 cache-read/cache-write detail is missing or malformed, the original reservation remains active rather than assuming zero cost.

```bash
# Retry a definitive failure such as HTTP 429
./aaron-reader ai retry 37 --yes

# Retry only after accepting that the unknown request may already have been billed
./aaron-reader ai retry 37 --allow-unknown --yes
```

Worker startup safely releases an old reservation that never reached the "sent" state. An old sent request is instead converted to `unknown`. AI failures do not change source health, mark articles read, or block deterministic syncing.

To remove old cached outputs while retaining usage/hash audit records:

```bash
./aaron-reader ai purge --before 2026-01-01 --yes
./aaron-reader ai purge --before 2026-01-01 --keep-snapshots --yes
```

### 6. Optional local page buttons

First set `ai.features.web_actions=true`, keep `ai.enabled=true`, and provide `OPENAI_API_KEY` to the server process. Then explicitly start:

```bash
./aaron-reader serve --open --enable-ai-actions
```

The live page gets summary/translation buttons only for that server run; ordinary `render` and the on-disk `public/index.html` remain passive, so stopping the server cannot leave callable controls behind. Page load performs only a local session bootstrap and never generates content. A click submits a bounded article ID/task/language request to the Python backend; the browser cannot select a provider, model, endpoint, prompt, URL, or API key. The write API requires loopback binding and client address, exact Host, same Origin on POST, a per-startup CSRF secret, strict JSON under 4 KiB, and a bounded client request ID for duplicate-submission detection. AI actions are rejected with `--allow-network`.

Cached AI results remain visible in ordinary static HTML and `latest.json`, clearly labeled as AI-generated, target language, metadata/full-text basis, generation date, and truncation state. Merely displaying, filtering, or changing interface language never calls a model.

## Minimal input packets for an LLM

If you later need a model to produce a Chinese summary, do not send the full pages to it again. First export only unread article titles and publisher-provided descriptions, with a hard character budget:

```bash
./aaron-reader packet --max-chars 6000 > /tmp/aaron-reader-packet.json
```

`packet` does not call a model. `character_budget` covers the entire formatted JSON document, including outer fields and newlines. `character_count` and `utf8_bytes` report the actual serialized size; `approx_tokens` is only a rough estimate. Tokenizers differ, so the program does not pretend to calculate an exact billable token count. Syncing cannot add the same article to the database twice. The recommended workflow is:

1. Let deterministic code sync, deduplicate, search, and filter.
2. Review titles and official descriptions first.
3. Only when necessary, send the small `packet` object to an LLM once.
4. Mark completed articles with `read` so the next packet excludes them.

## Outputs and data

- `data/reader.sqlite3`: articles, source state, HTTP validators, the persistent pending queue, notification outbox, and sync history.
- `data/reader.sqlite3` also contains AI jobs, per-attempt usage/reservations, cached AI artifacts, durable daily/weekly report records, and—only with the cached full-text policy—normalized content snapshots. It never contains the API key.
- `public/index.html`: exact whole-database counts, search, source/unread filters, and cards for the latest 500 articles.
- `public/latest.json`: the latest 500 articles for other deterministic programs, with explicit returned and omitted counts plus cached `ai_reports` for the latest daily/weekly report in each language.
- `public/feed.xml`: a local RSS feed containing the latest 100 articles across all four sources.
- `public/digest.md`: a deterministic Markdown digest of the latest 100 unread articles, with the exact total unread count.

These runtime artifacts are excluded by `.gitignore` by default. Outputs are written to temporary files and atomically replaced. Syncs use a process lock and SQLite WAL, so repeated runs are idempotent.

When backing up a live WAL database, do not copy only the main `reader.sqlite3` file. Either stop the LaunchAgent and copy the database together with its `-wal` and `-shm` files, or use the SQLite backup API. The simplest personal backup procedure is to uninstall the scheduled job, confirm that no `sync` process is running, and then copy the entire `data` directory.

## Fetch strategy

| Source | Deterministic entry point | Incremental behavior and deduplication |
|---|---|---|
| OpenAI News | Official RSS 2.0 feed | Diffs `guid` and canonical URLs across the complete RSS feed; display backfill is separated from discovery of new URLs |
| OpenAI Developer Blog | Official lightweight `blog.md` index | HTTP ETag/Last-Modified plus a full Markdown link diff; the HTML listing supplies dates, and failures remain eligible for retry |
| Claude Blog | HTML listing plus official sitemap | Homepage validators; daily sitemap URL set diff; only new URLs trigger JSON-LD/OG article fetches |
| Anthropic News | HTML listing plus official sitemap | Daily sitemap URL/lastmod set diff; new URLs fetch details, while lastmod changes refresh content without duplicate notifications |

On their first run, the Claude and Anthropic sources register historical sitemap URLs as known without fetching every detail page. Later sitemap diffs still find URLs published while the machine was offline, even when the homepage shows only a limited selection. Only exact `/blog/<slug>` or `/news/<slug>` paths are accepted; section pages, category pages, and unexpected redirects are not treated as articles.

Every new URL and lastmod change is written to a persistent SQLite queue before detail enrichment begins. Work is completed in bounded batches: at most 25 sitemap items and 200 direct-feed items per sync. A transient detail-page failure, process exit, unchanged response body, or a later sitemap `304` cannot erase queued work; an item is acknowledged only after its article is successfully committed. `status` shows pending counts and sitemap/enrichment errors, while `status --strict` is suitable for unattended monitoring.

The HTTP layer includes:

- `ETag` / `If-None-Match`
- `Last-Modified` / `If-Modified-Since`
- SHA-256 response-body hashes when no validator is available
- A 25-second timeout and an 8 MB response limit
- Bounded retries for 429/5xx responses; long `Retry-After` values persist backoff across syncs, and the first 429 stops remaining detail requests to that site for the current sync
- A per-host request interval
- Independent source failures, so one source does not block the others
- Rejection of suspiciously large parser-result drops to prevent an upstream redesign from corrupting state

Remote article removal does not delete local articles. Updating an article title or description preserves its read and starred state.

New articles and pending notification records are committed in the same database transaction. If a macOS notification temporarily fails, the outbox retries it during a later sync. The article remains unread even when notifications are unavailable, so the reading page and `packet` still include it.

## Configuration and extension

Source configuration lives in `config/sources.json`. RSS and Atom sources can reuse the `rss` adapter. Web sources should get an explicit, testable deterministic adapter instead of asking an LLM to guess the DOM at runtime.

The runtime uses only the Python standard library. Run offline tests with:

```bash
make test
```

Fixture tests mock both source and provider transports; they never access the network, call an LLM, or consume tokens. Run `doctor --live` explicitly to check the four upstream parser contracts and detect site-structure changes early. The normal `doctor` command performs local checks only.

## Design boundaries

- The reader stores publisher-provided titles, descriptions, categories, and links; it does not mirror full article text by default. Full-text snapshots require a separate explicit policy and command.
- It does not bypass authentication, Cloudflare challenges, or robots restrictions.
- Built-in AI enrichment is disabled by default, never runs from `sync`, and requires explicit feature/configuration plus a user command or protected loopback click. It enforces versioned content-hash caching, bounded inputs/outputs, hard daily/monthly budgets, and per-attempt audit records.
- The reading page is read-only by default. `read`, `unread`, `star`, and `unstar` regenerate it automatically after a change; you can also run `render` manually. Token-consuming buttons exist only during an explicitly enabled loopback server run.
