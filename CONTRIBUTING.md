# Contributing to Aaron Reader

Thanks for helping improve Aaron Reader. The project favors deterministic,
auditable code over model calls: collection, parsing, rendering, testing, and
deployment preparation must work with zero LLM tokens. Language artifacts use
the closed OpenRouter Free profile by default for per-article Simplified
Chinese translations, with the closed DeepSeek V4 Flash profile as a bounded
one-way fallback.

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

Provider changes must preserve the closed profile allowlist, strict structured
output validation, bounded usage accounting, and the no-replay generation-hold
behavior. The only automatic cross-provider path is the audited one-way
OpenRouter-to-DeepSeek policy: a missing pre-send OpenRouter key; explicit
401/402/404/429 responses; the closed typed availability codes
`rate_limit_exceeded`, `provider_overloaded`, and `provider_unavailable`; the
closed profile codes `thinking_output`, `thinking_tokens`, and `tool_calls`;
or locally invalid structured output. Every eligible typed, profile-violation,
or local-output path requires complete usage, and unknown codes are denied.
Timeouts, ambiguous transport or 5xx results, unknown usage, existing holds,
other local input/configuration errors, budget failures, and safety/policy
refusals must remain ineligible for cross-provider fallback. A
`fallback_pending` hold is the sole narrow hold exception for that continuation
and may authorize only the configured DeepSeek call, never a primary-provider
replay. Never combine two provider transmissions into one attempt, propagate
broad `force_held` authority to fallback, add a third call, or fall back in
reverse.

The scheduled translation backfill is a separate paid-failure replay policy. It
must scan the bounded current corpus for missing translations up to the
configured per-cycle processing limit, isolate one `AIServiceError` from later
articles, and grant at most one replay on the currently active provider profile
per article per scheduled cycle. That authorization is valid only when every
semantically equivalent hold is `paid_failure`; one equivalent `ambiguous` hold
must block automatic replay. The replay and any one existing
OpenRouter-to-DeepSeek continuation consume the normal daily budget. A
persistently definite paid failure may receive one new billable replay in each
later scheduled cycle; it stops automatically if the result becomes ambiguous
or the budget gate closes. Manual runs must leave this narrow policy disabled
by default. `force_held` remains an explicit broad recovery control that may
bypass ambiguous holds; tests and documentation must not conflate it with the
scheduled all-paid-failure rule.
Every strictly valid artifact, ledger update, and hold produced before an
incomplete cycle must be published atomically, while the final job still fails
visibly if any article failed or remained held.

OpenRouter Free dynamically chooses an upstream free model, so tests must not
assume one resolved model and production prompts must remain limited to public
publisher metadata. Its internal upstream routing is distinct from Aaron
Reader's application-level fallback. Never add private, personal, or sensitive
data to a model request. Provider-backed article-translation artifacts must
commit with attempt completion in one transaction; a sent attempt must always
create its provisional no-replay hold atomically. Production depends on the
serialized single-writer workflow, and contributors must not claim an
exactly-once guarantee across complete hosted-runner loss.

## Public snapshot boundary

The committed crawler handoff, AI cache, and files under `site/data/` and
`site/public/reader/` are public deployment inputs. Their fixed serializers
remove personal state, raw errors, full text, model responses, credentials, and
internal attempt data before staging them. Do not bypass those projections or
force-add ignored runtime data.

Keep pull requests focused, explain behavior changes, and include the checks
you ran. Security issues should follow `SECURITY.md`, not a public issue.
