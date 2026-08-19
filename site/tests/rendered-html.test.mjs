import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker;
}

async function render(pathname = "/") {
  const worker = await loadWorker();

  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("serves RSS with the active Cloudflare origin", async () => {
  const worker = await loadWorker();
  const canonical = "https://aaron-reader.aaron-he-zhu.workers.dev/";
  const response = await worker.fetch(
    new Request("https://reader-preview.example/reader/feed.xml"),
    {
      ASSETS: {
        fetch: async () => new Response(
          `<?xml version="1.0"?><rss><channel><link>${canonical}</link></channel></rss>`,
          { status: 200 },
        ),
      },
    },
    { waitUntil() {}, passThroughOnException() {} },
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/rss+xml; charset=utf-8");
  assert.match(await response.text(), /<link>https:\/\/reader-preview\.example\/<\/link>/);
});

test("server-renders the Aaron Reader snapshot", async () => {
  const snapshot = JSON.parse(
    await readFile(new URL("../data/latest.json", import.meta.url), "utf8"),
  );
  assert.equal(
    Object.hasOwn(snapshot, "ai_reports"),
    false,
    "The public snapshot must not expose removed AI brief payloads",
  );
  assert.equal(
    Object.hasOwn(snapshot, "cached_ai_report_count"),
    false,
    "The public snapshot must not expose the removed AI brief counter",
  );
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Aaron Reader — AI labs, without the noise<\/title>/i);
  assert.match(
    html,
    /<link rel="canonical" href="https:\/\/aaron-reader\.aaron-he-zhu\.workers\.dev\/"\s*\/?>/i,
  );
  assert.match(
    html,
    /<meta property="og:url" content="https:\/\/aaron-reader\.aaron-he-zhu\.workers\.dev\/"\s*\/?>/i,
  );
  assert.doesNotMatch(html, /aaron-reader\.zhuhe1983\.workers\.dev/i);

  assert.match(html, /Official OpenAI and Anthropic posts/);
  assert.match(html, /click through to read the original/);
  assert.match(html, /Updated twice a day/);
  assert.match(html, /Chinese is added automatically/);

  assert.doesNotMatch(html, /Independent signal desk/);
  assert.doesNotMatch(html, /The work behind/);
  assert.doesNotMatch(html, /deterministic feed/);
  assert.doesNotMatch(html, /0 LLM tokens|LLM tokens for collection/);
  assert.doesNotMatch(html, /Built for signal, not engagement/);
  assert.doesNotMatch(html, /Default · OpenRouter Free → DeepSeek V4 Flash/);
  assert.doesNotMatch(html, /can switch once to DeepSeek V4 Flash/);
  assert.doesNotMatch(html, /explicitly selected DeepSeek-only run/);
  assert.doesNotMatch(html, /Ambiguous network results/);
  assert.doesNotMatch(html, /OpenRouter Free may resolve to/);
  assert.doesNotMatch(html, /Chinese AI automation coverage/);

  assert.doesNotMatch(html, /class="ai-summary-card"/);
  assert.doesNotMatch(html, /Translate to Chinese|翻译为中文/);
  assert.doesNotMatch(html, /Backfill 3 articles|补齐接下来 3 篇/);
  assert.doesNotMatch(html, /codex:\/\//i);
  assert.doesNotMatch(html, /codex-center|codex-action|article-ai-tools|article-ai-action/i);
  assert.doesNotMatch(html, /aaron-reader-workspace-path|NEXT_PUBLIC_CODEX_WORKSPACE_PATH/i);
  assert.doesNotMatch(html, /on-demand ai|workspace path|local checkout/i);
  assert.doesNotMatch(html, /signed-in ChatGPT subscription/i);
  assert.doesNotMatch(html, /Summarize (?:today|this week|in English)/);
  assert.doesNotMatch(html, /id="report-section-title"/);
  assert.doesNotMatch(
    html,
    /class="[^"]*\breport-(?:section|grid|card|items|footer)\b/,
    "SSR must not render the removed AI brief UI",
  );
  assert.match(html, /OpenAI News/);
  assert.match(html, /Anthropic News/);
  assert.doesNotMatch(html, /codex-preview/);
  assert.doesNotMatch(html, /appgprj_|chatgpt\.site|\/Users\/|\.openai\/hosting\.json/);
  assert.doesNotMatch(html, /Your site is taking shape/);
});
