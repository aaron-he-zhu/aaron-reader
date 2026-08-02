# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not open a public issue containing credentials, private feed data, workflow
payloads, or details that make an unpatched issue easier to exploit.

Never send a DeepSeek API key, Cloudflare token, GitHub token, SQLite database,
`.env`/`.dev.vars` file, or GitHub Actions artifact as part of a report. Provide
the smallest synthetic reproduction that demonstrates the problem.

## Supported version

Security fixes target the current `main` branch. The hosted Cloudflare Worker
is a read-only projection and intentionally exposes no model endpoint, API key,
database, D1, R2, or write API.
The DeepSeek credential is accepted only as the encrypted GitHub Actions secret
`DEEPSEEK_API_KEY` and must never be committed.
