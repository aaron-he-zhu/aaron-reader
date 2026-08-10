# Aaron Reader on Cloudflare Workers

Production: [aaron-reader.aaron-he-zhu.workers.dev](https://aaron-reader.aaron-he-zhu.workers.dev/)

This directory is the isolated Cloudflare Worker deployment surface for Aaron Reader. The
Python application in the parent directory remains the only data producer. The
site imports its deterministic JSON, RSS, and Markdown outputs at build time.

```bash
npm ci --ignore-scripts --prefer-offline --no-audit --no-fund
npm run dev
npm run lint
npm run typecheck
npm test
npm run deploy
```

The site has no D1 or R2 binding, contains no API key, and exposes no browser-
callable AI endpoint. Chinese article translations, plus English and Chinese
daily reports, are refreshed in the twice-daily cloud pipeline by OpenRouter
Free by default, with a bounded one-way DeepSeek V4 Flash fallback. Only
explicitly classified failures can switch the current run to DeepSeek;
timeouts, ambiguous results, unknown usage, and safety or policy refusals fail
closed. Weekly reports are generated once on Sunday evening. All results are
published as validated cached artifacts. The hosted reader only renders those
caches; loading or using the page never calls a model.

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
publish a separate per-article AI summary. Published reports require an exact
language match, and their per-item notes remain distinct from independently
cached article translations. Report details use collapsed native disclosures so
the archive remains the primary reading surface.
