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

The site has no D1 or R2 binding, contains no API key, and exposes no hosted AI
endpoint. Optional AI summaries and translations are generated locally under
the parent application's explicit opt-in and hard-budget rules, then displayed
only as cached artifacts in a later snapshot.

`../scripts/prepare_cloudflare_release.py` performs the repeatable release
preparation. Source and snapshots are pushed to GitHub before Cloudflare
Workers Builds deploys that exact commit. The repository contains no
Cloudflare credential, model credential, SQLite database, or hosted AI API.

The `codex://` action panel lets each visitor save an optional absolute checkout
path in browser-local storage. Public source and deployment snapshots never
contain that path. For a private local build, `.env.example` can seed the same
value through an ignored `.env.production.local` file.

Summary and daily/weekly report actions follow the active interface language.
Translation and the optional three-article historical backfill are explicitly
Simplified Chinese. Published reports require an exact language match, and
their per-item notes remain distinct from independently cached article
artifacts. Report details and per-article AI tools use collapsed native
disclosures so the archive remains the primary reading surface.
