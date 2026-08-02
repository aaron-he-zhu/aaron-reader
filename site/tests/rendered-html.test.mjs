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
  const articleCount = snapshot.articles.length;
  const coveredArticleCount = snapshot.articles.filter((article) => {
    const artifacts = article.ai_artifacts || [];
    const hasSummary = artifacts.some(
      (artifact) => artifact.task === "summary" && artifact.target_language === "zh-CN",
    );
    const hasTranslation = artifacts.some(
      (artifact) => artifact.task === "translation" && artifact.target_language === "zh-CN",
    );
    return hasSummary && hasTranslation;
  }).length;
  const expectedEnglishReports = (snapshot.ai_reports || []).filter(
    (report) => report.target_language === "en" || report.output?.language === "en",
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
  assert.match(html, /Independent signal desk/);
  assert.match(html, /The work behind/);
  assert.match(html, /DeepSeek V4 Flash · automatic twice daily/);
  assert.match(html, /Chinese AI automation coverage/);
  assert.match(
    html,
    new RegExp(`<strong>${coveredArticleCount}(?:<!-- -->)?\\/(?:<!-- -->)?${articleCount}<\\/strong>`),
  );
  if (coveredArticleCount === articleCount) {
    assert.match(html, /All articles have a cached Chinese summary and translation/);
  } else {
    assert.match(html, /Cloud automation will fill missing summaries and translations/);
  }
  assert.doesNotMatch(html, /Translate to Chinese|翻译为中文/);
  assert.doesNotMatch(html, /Backfill 3 articles|补齐接下来 3 篇/);
  assert.doesNotMatch(html, /codex:\/\//i);
  assert.doesNotMatch(html, /codex-center|codex-action|article-ai-tools|article-ai-action/i);
  assert.doesNotMatch(html, /aaron-reader-workspace-path|NEXT_PUBLIC_CODEX_WORKSPACE_PATH/i);
  assert.doesNotMatch(html, /on-demand ai|workspace path|local checkout/i);
  assert.doesNotMatch(html, /signed-in ChatGPT subscription/i);
  assert.doesNotMatch(html, /Summarize (?:today|this week|in English)/);
  assert.match(html, /GitHub Actions · 10:00 &amp; 22:00 San Francisco/);
  const reportCards = html.match(
    /<article\b(?=[^>]*\bclass="[^"]*\breport-card\b[^"]*")[^>]*>[\s\S]*?<\/article>/g,
  ) ?? [];
  if (expectedEnglishReports.length > 0) {
    assert.match(html, /class="report-section"/);
    for (const report of expectedEnglishReports) {
      const label = report.period === "daily" ? "Daily brief" : "Weekly brief";
      assert.ok(
        reportCards.some(
          (card) => /\blang="en"/.test(card) && card.includes(`<strong>${label}</strong>`),
        ),
        `English SSR should render its ${report.period} report card`,
      );
    }
  } else {
    assert.equal(reportCards.length, 0);
  }
  assert.ok(
    reportCards.every((card) => !/\blang="zh-CN"/.test(card)),
    "English SSR must not render a visible zh-CN report card",
  );
  const reportDetailTags = reportCards.flatMap(
    (card) => card.match(
      /<details\b(?=[^>]*\bclass="[^"]*\breport-items\b[^"]*")[^>]*>/g,
    ) ?? [],
  );
  if (reportCards.length > 0) {
    assert.ok(reportDetailTags.length > 0, "Published reports should include source notes");
  }
  for (const tag of reportDetailTags) {
    assert.doesNotMatch(tag, /\sopen(?:\s|=|>)/i, "Report details should be collapsed by default");
  }
  assert.match(html, /OpenAI News/);
  assert.match(html, /Anthropic News/);
  assert.doesNotMatch(html, /codex-preview/);
  assert.doesNotMatch(html, /appgprj_|chatgpt\.site|\/Users\/|\.openai\/hosting\.json/);
  assert.doesNotMatch(html, /Your site is taking shape/);
});
