# Aaron Reader

[简体中文](README.zh-CN.md)

[Live site](https://aaron-reader.aaron-he-zhu.workers.dev/) · [GitHub repository](https://github.com/aaron-he-zhu/aaron-reader)

Aaron Reader is a bilingual, cloud-hosted reader for official OpenAI and Anthropic publications. GitHub Actions collects and verifies new articles; OpenRouter Free translates each article's title and publisher summary into Simplified Chinese, with DeepSeek V4 Flash as a bounded fallback; and Cloudflare Workers serves the resulting read-only snapshot.

Production runs entirely on GitHub and Cloudflare. It does not depend on a personal computer, a local database, a desktop application, or an interactive AI subscription.

## Sources

- [OpenAI News](https://openai.com/news/rss.xml)
- [OpenAI Developer Blog](https://developers.openai.com/blog)
- [Claude Blog](https://claude.com/blog/)
- [Anthropic News](https://www.anthropic.com/news)

English is the default interface. The language selector provides a complete Simplified Chinese interface and automatically displays cached Chinese translations when they are available. The website has no client-side translation button, and it does not generate a separate per-article AI summary.

## Cloud architecture

```text
Official publisher feeds and pages
                │
                ▼
GitHub Actions — 09:15 and 21:15 America/Los_Angeles
  1. Restore public crawler and AI cache state into an ephemeral database
  2. Crawl, parse, canonicalize, fingerprint, deduplicate, and validate
  3. Ask OpenRouter Free only for missing or content-changed language artifacts, with a one-way DeepSeek fallback
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

The update workflow runs every day at **09:15 and 21:15 in `America/Los_Angeles`**. The named timezone is intentional: GitHub applies the San Francisco daylight-saving transition instead of relying on a fixed UTC offset. These times map to 00:15 and 12:15 Beijing time during Pacific daylight time, and 01:15 and 13:15 during Pacific standard time, keeping the scheduled runs outside DeepSeek's announced 09:00–12:00 and 14:00–18:00 Beijing peak windows.

Each successful cycle:

- checks all four sources with deterministic code;
- reuses valid artifacts already bound to the current article content hash;
- translates only the title and publisher summary for each article missing a current Simplified Chinese translation; and
- publishes a new snapshot only after all required checks pass.

Everything outside that language-understanding task uses fixed code and consumes no LLM tokens: HTTP caching, parsing, URL normalization, article identity, content hashing, deduplication, source health, cache selection, budget enforcement, serialization, rendering, tests, commits, and deployment preparation.

## Fixed AI provider profiles

Production uses the closed OpenRouter Free profile by default and the closed DeepSeek V4 Flash profile as its only automatic fallback. `config.ai.provider` selects the primary profile; `ai cloud-run --provider ...` can explicitly override it for one run. A manually selected DeepSeek run is DeepSeek-only and never falls back in reverse.

| Profile | Fixed requested model | GitHub Actions secret | Resolution behavior |
| --- | --- | --- | --- |
| DeepSeek V4 Flash | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` | DeepSeek serves the named model. |
| OpenRouter Free | `openrouter/free` | `OPENROUTER_API_KEY` | OpenRouter dynamically selects an eligible free model and returns that concrete model in the response. |

The endpoints, requested models, credential environment names, non-reasoning mode, structured-output contract, and lack of model tools are fixed by code. The requested and resolved model identities are audited separately, which is essential for the dynamic OpenRouter profile.

Aaron Reader's application-level fallback is one-way and deliberately conservative. Before a request is sent, a missing OpenRouter credential may switch directly to DeepSeek. After a request is sent, automatic fallback is allowed only for an explicit OpenRouter `401`, `402`, `404`, or `429`; a terminal OpenRouter `rate_limit_exceeded`, `provider_overloaded`, or `provider_unavailable` error with complete usage; or the closed profile violations `thinking_output`, `thinking_tokens`, and `tool_calls`, again with complete usage. A locally invalid structured completion can also fall back only when its usage is complete. Unknown or future provider error codes are denied by default. The failed OpenRouter request and the DeepSeek continuation are separate jobs and attempts with independent idempotency keys, budget entries, requested models, and provenance.

The first eligible OpenRouter failure trips a one-way circuit breaker for that cloud run: DeepSeek handles the current work item once and remains active for the rest of the run. There is no third provider call and no DeepSeek-to-OpenRouter loop. The next scheduled run probes OpenRouter again. If the process or budget stops between the two providers, a durable `fallback_pending` hold makes the next successfully exported run continue on DeepSeek without replaying OpenRouter.

Ambiguous results never trigger fallback. Timeouts, connection failures, `408`/`409`/`425`, `5xx`, malformed, untyped, conflicting, or truncated provider responses, unknown usage, pre-existing ambiguous or paid-failure generation holds, budget failures, all other local configuration/input errors, and safety, moderation, content-filter, or policy refusals all fail closed. The sole configuration exception is the missing pre-send OpenRouter credential described above. Definite non-fallback request/policy failures such as `400`, `403`, and `422` create a cross-profile paid-failure hold. A `fallback_pending` hold is the sole narrow hold exception: it authorizes only the configured DeepSeek continuation and never a replay on the primary provider. An ambiguous or paid non-fallback hold applies to the same semantic work across both profiles, so changing provider cannot bypass it; replay requires an explicit provider selection and the `force_held` acknowledgement.

Immediately before each provider POST, the local attempt state and an `ambiguous` no-replay hold are committed in one SQLite transaction. A definitive response atomically settles that provisional hold, and the validated article-translation artifact, usage, and attempt completion commit together. Those protections survive Python/CLI failures once the workflow exports and publishes `cloud/ai-cache.json`. They are not an absolute exactly-once guarantee if the entire hosted runner and its unexported local database disappear after the provider receives a request; neither configured provider is assumed to offer a server-side idempotency guarantee. The production workflow is serialized to one writer, and the hold settlement code relies on that single-writer boundary.

`openrouter/free` is a dynamic zero-cost routing target, not a single deterministic model or a reliability guarantee. Its eligible model pool, availability, latency, output characteristics, upstream provider, and upstream data-handling policy can change. OpenRouter receives the request and may route or fail over among its eligible upstream providers; Aaron Reader's one-way rule governs only the separate OpenRouter-to-DeepSeek continuation performed by this application. Aaron Reader therefore sends only bounded public publisher metadata, never personal reading state or private content. Operators should review OpenRouter's current [Free Models Router](https://openrouter.ai/docs/guides/routing/routers/free-router), [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection), and [provider data-policy controls](https://openrouter.ai/docs/guides/privacy/provider-logging/) before enabling that profile and must not use it for confidential, personal, or otherwise sensitive input.

## Persistent public state

GitHub-hosted runners are disposable, so the repository contains two deliberately small, public continuation files:

- `crawler/latest.json` contains the strict crawler handoff: source identity, safe article metadata, content fingerprints, and bounded fetch-continuation data. It contains no SQLite database, personal read/star state, credentials, or raw failure history.
- `cloud/ai-cache.json` contains validated translations and legacy article-summary artifacts retained for compatibility, keyed by stable publisher identity and content hash rather than temporary database row IDs. New production runs do not create per-article summaries, and the public reader projection omits those legacy artifacts. The cache also carries a bounded, aggregate San Francisco usage ledger and input-free generation holds so budgets and no-replay safety survive disposable runners. It contains no API key, provider request ID, request-level audit, error text, prompt, model response, private reading state, or extracted full article text.

At the beginning of a run, both files are validated and imported into a fresh ephemeral SQLite database. At the end, fixed serializers export the next public state atomically. Generated deployment data under `site/data/` and `site/public/reader/` is another public projection; runtime databases and temporary files are ignored by Git.

Before a release is committed, the two exported handoffs are imported once more into a second empty validation database. The website is rendered only from that reconstructed database, and its generated public snapshot is validated before release. This makes the checked-in state sufficient to reproduce the deployed site on a different disposable runner.

Because AI artifacts are content-hash-bound, an unchanged article remains a cache hit across runs. Valid historical artifacts can also be reused after a provider or model change; switching the configured model does not by itself force every article to be regenerated.

## Model credentials

Configure both fixed-profile credentials as GitHub Actions repository secrets:

```text
DEEPSEEK_API_KEY
OPENROUTER_API_KEY
```

Configure them in **GitHub repository → Settings → Secrets and variables → Actions → New repository secret**. Enter each key only in GitHub's secret-value field; never place a key in a file, workflow input, issue, commit, build log, or Cloudflare variable.

The workflow makes credentials available only inside the bounded AI-generation step. The default OpenRouter path receives the separately named `OPENROUTER_API_KEY` and `DEEPSEEK_API_KEY` so the fixed one-way fallback can run; an explicitly selected DeepSeek-only run receives no OpenRouter credential. The keys are never merged into a generic credential or wired to the other provider's endpoint. Provider endpoints and requested models are fixed by code, model output must satisfy a strict JSON contract and local validation, and the model receives no tools. The browser, Cloudflare Worker, public snapshots, pull-request checks, and deterministic crawler never receive either key.

Forks do not inherit repository secrets. A fork must add its own `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` before both profiles are available to its production update workflow.

## Token, budget, and failure behavior

Aaron Reader minimizes billable work at several layers:

- a newly changed article requests only one bounded translation of its title and publisher summary;
- exact content-hash cache hits skip the provider, including compatible historical artifacts;
- a bounded aggregate ledger carries confirmed use and conservative unknown-result reservations across runners, so daily and monthly request/token caps cannot reset with each ephemeral job;
- article counts, input characters, output tokens, response size, request count, total tokens, timeouts, and worker concurrency are bounded by configuration;
- reasoning is disabled in both fixed profiles for this structured transformation workload;
- every real OpenRouter or DeepSeek request has its own audited attempt and consumes the shared request/token budget;
- a fallback-eligible OpenRouter failure can add exactly one DeepSeek request and visibly marks the run as degraded; and
- an ambiguous network result, unknown usage, or safety/policy refusal never falls back and creates the appropriate stable generation hold, so future scheduled runs do not silently replay the workload.

Source-health, state-schema, privacy, test, repository-boundary, or site-build failures are fail-closed and publish nothing. An incomplete AI cycle is handled differently to avoid charging twice for work that already succeeded: invalid model output is discarded, but every earlier artifact, aggregate usage update, and generation hold that passed strict validation is exported and published before the workflow raises a failed alert. Missing credentials, provider errors, and exhausted budgets therefore never publish unvalidated output, while safely completed partial progress remains reusable on the next run.

## Running an update manually

Open the repository's **Actions** tab, select the production update workflow under `.github/workflows/`, choose **Run workflow**, leave the default `openrouter` primary or select `deepseek` for a DeepSeek-only diagnostic run, and run it on `main`. A manual run uses the same ephemeral database, secret boundary, cache checks, safety limits, tests, exact-file commit, and Cloudflare deployment path as a scheduled run. `force_held` is a recovery control that may repeat a previously billed generation; leave it disabled unless you have reviewed the workflow summary and intentionally accept that cost. Scheduled runs never enable this override.

The workflow summary is the operational record: it reports cache hits, provider calls and token usage, failures, changed public files, and whether a commit was published. Secret values and provider response bodies are not logged.

## Deploying a fork

To host an independent copy:

1. Fork the repository and allow the production workflow to write repository contents.
2. Add the GitHub Actions repository secret for each profile the fork will use: `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, or both.
3. Connect the fork to Cloudflare Workers Builds, using `main` as the production branch and `site/` as the application root.
4. Keep the checked-in lockfile and `site/wrangler.jsonc`; no model secret or AI binding is needed in Cloudflare.
5. Add a GitHub Actions repository variable named `PUBLIC_SITE_URL` containing the fork's credential-free HTTPS origin.
6. Set the optional GitHub Actions repository variable `AI_PROVIDER` to `openrouter` or `deepseek` for scheduled runs; if omitted, scheduled runs use `openrouter` with the fixed DeepSeek fallback.
7. Enable the scheduled production workflow and run it once manually to validate the complete path.

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
