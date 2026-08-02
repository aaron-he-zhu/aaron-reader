import assert from "node:assert/strict";
import test from "node:test";

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the Aaron Reader snapshot", async () => {
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
  assert.match(html, /Independent signal desk/);
  assert.match(html, /The work behind/);
  assert.match(html, /GPT-5\.6 Luna/);
  assert.match(html, /Brief it with Codex/);
  assert.match(html, /Summarize today in English/);
  assert.match(html, /Summarize this week in English/);
  assert.match(html, /Backfill 3 articles in Chinese/);
  assert.match(html, /AI cache coverage/);
  assert.match(html, /Summarize in English/);
  assert.match(html, /Translate to Chinese/);
  assert.match(html, /AI tools/);
  assert.match(html, /prefilled prompt/);
  assert.match(html, /press Send/);
  assert.match(html, /Local Aaron Reader checkout/);
  assert.match(html, /codex:\/\/new\?prompt=/);
  assert.match(html, /10:00 &amp; 22:00 San Francisco/);
  assert.match(html, /class="report-section"/);
  assert.match(html, /<article class="report-card" lang="en">/);
  assert.match(html, /<strong>Weekly brief<\/strong>/);
  assert.match(html, /<details class="report-items"><summary>/);
  assert.doesNotMatch(html, /<details class="report-items" open/);
  assert.doesNotMatch(html, /<article class="report-card" lang="zh-CN">/);
  assert.match(html, /OpenAI News/);
  assert.match(html, /Anthropic News/);
  assert.doesNotMatch(html, /codex-preview/);
  assert.doesNotMatch(html, /appgprj_|chatgpt\.site|\/Users\/|\.openai\/hosting\.json/);
  assert.doesNotMatch(html, /Your site is taking shape/);
});
