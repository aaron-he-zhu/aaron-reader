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
callable AI endpoint. Chinese summaries and translations, plus English and
Chinese daily reports, are refreshed by DeepSeek V4 Flash in the twice-daily
cloud pipeline. Weekly reports are generated once on Sunday evening. All
results are published as validated cached artifacts. The hosted reader only
renders those caches; loading or using the page never calls a model.

`../scripts/prepare_cloudflare_release.py` performs repeatable snapshot
validation and release preparation. Source and snapshots are pushed to GitHub
before Cloudflare Workers Builds deploys that exact commit. The repository
contains no Cloudflare credential, model credential, SQLite database, or
browser-callable AI API.

There are no client-side AI controls, deep links, workspace settings, or manual
translation buttons. Cached Simplified Chinese translations appear
automatically in the Chinese view, while cached article summaries remain visible
in the matching interface language. Published reports require an exact language
match, and their per-item notes remain distinct from independently cached
article artifacts. Report details use collapsed native disclosures so the
archive remains the primary reading surface.
