# Aaron Reader on Cloudflare Workers

Production: [aaron-reader.aaron-he-zhu.workers.dev](https://aaron-reader.aaron-he-zhu.workers.dev/)

This directory is the isolated Cloudflare Worker deployment surface for Aaron Reader. The
Python application in the parent directory remains the only data producer. The
site imports deterministic JSON and RSS outputs plus a deterministic Markdown
digest at build time.

```bash
npm ci --ignore-scripts --prefer-offline --no-audit --no-fund
npm run dev
npm run lint
npm run typecheck
npm test
npm run deploy
```

The site has no D1 or R2 binding, contains no API key, and exposes no browser-
callable AI endpoint. Chinese article translations are refreshed in the twice-
daily cloud pipeline by OpenRouter Free by default, with a bounded one-way
DeepSeek V4 Flash fallback. Only explicitly classified failures can switch the
current run to DeepSeek; timeouts, ambiguous results, unknown usage, and safety
or policy refusals fail closed. Translations are published as validated cached
artifacts. The hosted reader only renders those caches; loading or using the
page never calls a model.

Each scheduled producer run scans the bounded current corpus for articles that
still lack a translation, up to the configured per-cycle processing limit.
Article failures are isolated, so one `AIServiceError` does not stop later
missing articles. A schedule may retry once on the currently active provider
profile per article per cycle only when all semantically equivalent holds are
`paid_failure`; any `ambiguous` hold blocks automatic replay, and all attempts
remain subject to the daily budget. A persistently definite paid failure can
receive one new billable replay in each later scheduled cycle until it
succeeds, becomes ambiguous, or reaches that budget gate. The retry may still
use the existing single OpenRouter-to-DeepSeek continuation when a new
OpenRouter attempt is eligible. If the cycle has already switched to DeepSeek,
later articles stay on that active profile. Manual runs leave this narrow
policy off by default; the explicit broad `force_held` recovery can
bypass ambiguous holds and therefore carries duplicate-billing risk. Valid
partial progress is published before an incomplete producer job finishes red.

OpenRouter Free is a dynamic router rather than one deterministic model. Its
eligible free-model pool, upstream provider, availability, latency, output
characteristics, and upstream data policy can change. The producer therefore
sends only bounded public publisher metadata through this profile. Operators
must review current provider privacy terms and must not use it for confidential,
personal, or otherwise sensitive input. Neither `DEEPSEEK_API_KEY` nor
`OPENROUTER_API_KEY` is present in the site build or Cloudflare runtime.

`../scripts/prepare_cloudflare_release.py` performs repeatable snapshot
validation and release preparation. Source and snapshots are pushed to GitHub
before Cloudflare Workers Builds deploys that exact commit. The repository
contains no Cloudflare credential, model credential, SQLite database, or
browser-callable AI API.

There are no client-side AI controls, deep links, workspace settings, or manual
translation buttons. Cached Simplified Chinese title and publisher-summary
translations appear automatically in the Chinese view; the site does not
publish a separate per-article AI summary. Legacy article-summary artifacts may
remain in the producer cache for compatibility, but they are omitted from the
public reader projection. The archive remains the primary reading surface.
