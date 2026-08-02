# Contributing to Aaron Reader

Thanks for helping improve Aaron Reader. The project favors deterministic,
auditable code over model calls: collection, parsing, rendering, testing, and
deployment preparation must work with zero LLM tokens. DeepSeek is reserved for
summaries, Simplified Chinese translations, and daily or weekly reports.

## Development setup

- Python 3.9 or newer for the reader.
- Node.js 22.13 or newer for `site/`.
- No API key is needed for tests, deterministic syncs, or site builds.

Run the Python suite from the repository root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run the website checks from `site/`:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run lint
npm run typecheck
npm test
```

Tests must use bounded fixtures. Do not make live feed, model, GitHub, or
Cloudflare requests from the test suite. Never commit SQLite files, generated
root `public/` files, `.env*`, `.dev.vars*`, provider request/results, Wrangler
state, tokens, or credentials.

## Public snapshot boundary

The committed crawler handoff, AI cache, and files under `site/data/` and
`site/public/reader/` are public deployment inputs. Their fixed serializers
remove personal state, raw errors, full text, model responses, credentials, and
internal attempt data before staging them. Do not bypass those projections or
force-add ignored runtime data.

Keep pull requests focused, explain behavior changes, and include the checks
you ran. Security issues should follow `SECURITY.md`, not a public issue.
