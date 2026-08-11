# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not open a public issue containing credentials, private feed data, workflow
payloads, or details that make an unpatched issue easier to exploit.

Never send a DeepSeek or OpenRouter API key, Cloudflare token, GitHub token,
SQLite database, `.env`/`.dev.vars` file, or GitHub Actions artifact as part of
a report. Provide the smallest synthetic reproduction that demonstrates the
problem.

## Supported version

Security fixes target the current `main` branch. The hosted Cloudflare Worker
is a read-only projection and intentionally exposes no model endpoint, API key,
database, D1, R2, or write API.

Model credentials are accepted only as the encrypted GitHub Actions repository
secrets `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY`; neither may be committed.
Production selects the fixed OpenRouter Free profile by default and permits
only a fixed, one-way DeepSeek V4 Flash application-level fallback. The fallback is limited to a
missing OpenRouter credential before transmission; explicit OpenRouter 401,
402, 404, or 429 responses; the closed typed availability set
`rate_limit_exceeded` / `provider_overloaded` / `provider_unavailable`; and the
closed profile-violation set `thinking_output` / `thinking_tokens` /
`tool_calls`. Typed and profile failures require complete usage. A local
structured-output failure is eligible only when usage is complete; unknown or
future provider codes are denied. Each provider request receives a separate audited attempt,
budget reservation, idempotency key, model identity, and provenance record.

Timeouts, connection failures, 408/409/425, 5xx responses, malformed or
truncated provider responses, unknown usage, existing ambiguous or paid-failure
generation holds, other local input/configuration failures, exhausted budgets, and
safety, moderation, content-filter, abuse, or policy refusals must never trigger
cross-provider fallback. Those paths fail closed. A `fallback_pending` hold is
the sole narrow hold exception and authorizes only the configured DeepSeek
continuation, never a primary-provider replay. Fallback is never
DeepSeek-to-OpenRouter, never makes a third provider call, and never receives
the broad `force_held` override. The two credentials must remain in their
separately named variables and may coexist only in the bounded AI-generation
step.

Before a provider POST, the local attempt and provisional ambiguous hold must
commit atomically. The validated article-translation artifact must complete
with attempt state in the same transaction. This prevents replay after
process-level failures once the workflow successfully exports the public
handoff. It is not an exactly-once guarantee if the entire hosted runner and
its unexported SQLite state are lost after request transmission; do not assume
provider-side idempotency. Production must retain its serialized single-writer
workflow because provisional-hold settlement is designed for that boundary.

The OpenRouter Free profile sends bounded public publisher metadata to
OpenRouter, which dynamically selects an eligible free model and may route or
fail over among changing upstream providers. The application's one-way rule
governs only the subsequent OpenRouter-to-DeepSeek call, not OpenRouter's
internal routing. The free-model pool, provider
availability, and upstream retention or training policies can change. Do not
extend this profile to private feeds, extracted private text, personal reading
state, credentials, or any confidential, personal, or otherwise sensitive
input without a separate security and privacy review.
