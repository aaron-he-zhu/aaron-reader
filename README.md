# Aaron Reader

[简体中文](README.zh-CN.md)

[Live site](https://aaron-reader.aaron-he-zhu.workers.dev/) · [GitHub repository](https://github.com/aaron-he-zhu/aaron-reader)

Aaron Reader is a bilingual, cloud-hosted reader for official OpenAI and Anthropic publications. GitHub Actions collects and verifies new articles, DeepSeek creates only the summaries, Simplified Chinese translations, and briefs that require language understanding, and Cloudflare Workers serves the resulting read-only snapshot.

Production runs entirely on GitHub and Cloudflare. It does not depend on a personal computer, a local database, a desktop application, or an interactive AI subscription.

## Sources

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

English is the default interface. The language selector provides a complete Simplified Chinese interface and automatically displays cached Chinese translations when they are available. The website has no translate or summarize buttons because enrichment is automatic.

## Cloud architecture

```text
Official publisher feeds and pages
                │
                ▼
GitHub Actions — 10:00 and 22:00 America/Los_Angeles
  1. Restore public crawler and AI cache state into an ephemeral database
  2. Crawl, parse, canonicalize, fingerprint, deduplicate, and validate
  3. Ask DeepSeek only for missing or content-changed language artifacts
  4. Render and test the complete bilingual public snapshot
  5. Commit the exact safe state and snapshot files to main
                │
                ▼
Cloudflare Workers Builds
  Build the verified GitHub commit and publish the read-only site
```

The scheduled update is a single pipeline so the crawler state, AI cache, and public snapshot advance together. Concurrent runs are serialized, and a run cannot publish a partially updated combination of those files.

Cloudflare does not crawl publishers or call the model. The Worker only serves files that passed the GitHub workflow's schema, health, privacy, lint, type, build, and rendered-output checks.

## Schedule and AI cadence

The update workflow runs every day at **10:00 and 22:00 in `America/Los_Angeles`**. The named timezone is intentional: GitHub applies the San Francisco daylight-saving transition instead of relying on a fixed UTC offset.

Each successful cycle:

- checks all four sources with deterministic code;
- reuses valid artifacts already bound to the current article content hash;
- generates each missing Simplified Chinese summary and translation together in one shared-metadata `deepseek-v4-flash` request;
- evaluates English and Chinese daily briefs, generating both languages from one shared article window when both are missing and calling the model only when that validated input changed; and
- publishes a new snapshot only after all required checks pass.

The weekly brief is different from the daily brief: it covers the San Francisco calendar week, synthesizes longer-running themes across sources, and is generated **once on Sunday evening San Francisco time**. It is not regenerated during every twice-daily run. A manually dispatched update follows the same cache and validation rules.

Everything outside those language-understanding tasks uses fixed code and consumes no LLM tokens: HTTP caching, parsing, URL normalization, article identity, content hashing, deduplication, source health, cache selection, budget enforcement, serialization, rendering, tests, commits, and deployment preparation.

## Persistent public state

GitHub-hosted runners are disposable, so the repository contains two deliberately small, public continuation files:

- `crawler/latest.json` contains the strict crawler handoff: source identity, safe article metadata, content fingerprints, and bounded fetch-continuation data. It contains no SQLite database, personal read/star state, credentials, or raw failure history.
- `cloud/ai-cache.json` contains validated summaries, translations, and daily/weekly reports keyed by stable publisher identity and content hash rather than temporary database row IDs. It also carries a bounded, aggregate San Francisco usage ledger and input-free generation holds so budgets and no-replay safety survive disposable runners. It contains no API key, provider request ID, request-level audit, error text, prompt, model response, private reading state, or extracted full article text.

At the beginning of a run, both files are validated and imported into a fresh ephemeral SQLite database. At the end, fixed serializers export the next public state atomically. Generated deployment data under `site/data/` and `site/public/reader/` is another public projection; runtime databases and temporary files are ignored by Git.

Before a release is committed, the two exported handoffs are imported once more into a second empty validation database. The website is rendered only from that reconstructed database, and the release fails if its report identities differ from the public AI cache. This makes the checked-in state sufficient to reproduce the deployed site on a different disposable runner.

Because AI artifacts are content-hash-bound, an unchanged article remains a cache hit across runs. Valid historical artifacts can also be reused after a provider or model change; switching the configured model does not by itself force every article to be regenerated.

## DeepSeek secret

The only production model credential is the GitHub Actions repository secret named:

```text
DEEPSEEK_API_KEY
```

Configure it in **GitHub repository → Settings → Secrets and variables → Actions → New repository secret**. Enter the key only in GitHub's secret-value field; never place it in a file, workflow input, issue, commit, build log, or Cloudflare variable.

The workflow exposes this secret only to the bounded DeepSeek step. The provider endpoint and model are fixed by code, model output must satisfy a strict JSON contract and local validation, and the model receives no tools. The browser, Cloudflare Worker, public snapshots, pull-request checks, and deterministic crawler never receive the key.

Forks do not inherit repository secrets. A fork must add its own `DEEPSEEK_API_KEY` before its production update workflow can perform AI enrichment.

## Token, budget, and failure behavior

Aaron Reader minimizes billable work at several layers:

- summaries and Chinese translation for a newly changed article are requested together when both are missing;
- English and Chinese reports share one article-window request when both languages are missing, reducing a normal daily report from two calls to one and Sunday daily-plus-weekly reports from four calls to two;
- exact content-hash cache hits skip the provider, including compatible historical artifacts;
- report input hashes prevent unchanged daily or weekly windows from being regenerated;
- a bounded aggregate ledger carries confirmed use and conservative unknown-result reservations across runners, so daily and monthly request/token caps cannot reset with each ephemeral job;
- article counts, input characters, output tokens, response size, request count, total tokens, timeouts, and worker concurrency are bounded by configuration;
- DeepSeek reasoning is disabled for this structured transformation workload; and
- an ambiguous network failure or a paid-but-invalid result creates a stable generation hold, so future scheduled runs skip the exact workload without another model call and raise a visible workflow alert.

Source-health, state-schema, privacy, test, repository-boundary, or site-build failures are fail-closed and publish nothing. An incomplete AI cycle is handled differently to avoid charging twice for work that already succeeded: invalid model output is discarded, but every earlier artifact, aggregate usage update, and generation hold that passed strict validation is exported and published before the workflow raises a failed alert. Missing credentials, provider errors, and exhausted budgets therefore never publish unvalidated output, while safely completed partial progress remains reusable on the next run.

## Running an update manually

Open the repository's **Actions** tab, select the production update workflow under `.github/workflows/`, choose **Run workflow**, and run it on `main`. A manual run uses the same ephemeral database, secret boundary, cache checks, safety limits, tests, exact-file commit, and Cloudflare deployment path as a scheduled run. `force_weekly` is an explicit one-off weekly-report override. `force_held` is a separate recovery control that may repeat a previously billed generation; leave it disabled unless you have reviewed the workflow summary and intentionally accept that cost. Scheduled runs never enable either override.

The workflow summary is the operational record: it reports cache hits, provider calls and token usage, failures, changed public files, and whether a commit was published. Secret values and provider response bodies are not logged.

## Deploying a fork

To host an independent copy:

1. Fork the repository and allow the production workflow to write repository contents.
2. Add `DEEPSEEK_API_KEY` as a GitHub Actions repository secret.
3. Connect the fork to Cloudflare Workers Builds, using `main` as the production branch and `site/` as the application root.
4. Keep the checked-in lockfile and `site/wrangler.jsonc`; no model secret or AI binding is needed in Cloudflare.
5. Add a GitHub Actions repository variable named `PUBLIC_SITE_URL` containing the fork's credential-free HTTPS origin.
6. Enable the scheduled production workflow and run it once manually to validate the complete path.

The workflow derives its write boundary from GitHub's immutable current-repository context and verifies the checkout origin before pushing, so it can run in a fork without granting access to the upstream repository. Cloudflare should deploy only commits from `main`; preview URLs are intentionally disabled in the checked-in Worker configuration.

After every push, the workflow polls `PUBLIC_SITE_URL` and compares the deployed reader snapshot byte for byte with the verified commit. It succeeds only after Cloudflare has actually published that exact state. This check uses the public site and needs no Cloudflare credential.

## Development and verification

Development and CI checks can be run from any temporary checkout. No checkout, workstation process, or developer database is part of the production runtime.

Requirements:

- Python 3.9 or newer for the deterministic reader and tests;
- Node.js 22.13 or newer for the Cloudflare site; and
- no API key for tests, deterministic syncs, rendering, or site builds.

Run the Python suite from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run the website checks:

```bash
cd site
npm ci --ignore-scripts --no-audit --no-fund
npm run lint
npm run typecheck
npm test
```

For a deterministic parser check against the live publishers without persisting articles:

```bash
./aaron-reader doctor --live
```

Tests must not call live model, GitHub, or Cloudflare APIs. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution rules and [SECURITY.md](SECURITY.md) for reporting security issues.

## License

[MIT](LICENSE)
