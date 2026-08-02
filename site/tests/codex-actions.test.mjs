import assert from "node:assert/strict";
import test from "node:test";

import {
  CODEX_WORKSPACE_PATH,
  articleCodexLink,
  articleCodexPrompt,
  reportCodexLink,
  reportCodexPrompt,
} from "../app/codex-actions.ts";

test("article deep links select exactly one article task without an API key", () => {
  const workspacePath = "/workspace/aaron-reader";
  const url = new URL(articleCodexLink(110, "summary", workspacePath));
  const prompt = url.searchParams.get("prompt") ?? "";

  assert.equal(url.protocol, "codex:");
  assert.equal(url.hostname, "new");
  assert.equal(url.searchParams.get("path"), workspacePath);
  assert.equal(prompt, articleCodexPrompt(110, "summary"));
  assert.match(prompt, /Article ID: 110/);
  assert.match(prompt, /--task summary/);
  assert.match(prompt, /do not generate a translation artifact/i);
  assert.match(prompt, /signed-in ChatGPT subscription/);
  assert.match(prompt, /GPT-5\.6 Luna and medium reasoning/);
  assert.match(prompt, /Do not ask for, read, store, or use an OpenAI API key/);
  assert.match(prompt, /Run the export exactly once/);
  assert.match(prompt, /Read only the request_path/);
  assert.match(prompt, /exact suggested_result_path/);
  assert.match(prompt, /matching import exactly once/);
  assert.match(prompt, /public GitHub repository/);
  assert.match(prompt, /Cloudflare Workers Builds/);
  assert.match(prompt, /prepare_cloudflare_release\.py/);
  assert.doesNotMatch(prompt, /ChatGPT Site|Sites hosting|deploy_private_site_version/i);
  assert.doesNotMatch(prompt, /Cloudflare API token/i);
});

test("translation and period deep links carry distinct, bounded tasks", () => {
  const translation = articleCodexPrompt(7, "translation");
  const daily = reportCodexPrompt("daily");
  const weeklyUrl = new URL(reportCodexLink("weekly", "/workspace"));
  const weekly = weeklyUrl.searchParams.get("prompt") ?? "";

  assert.match(translation, /Article ID: 7/);
  assert.match(translation, /--task translation/);
  assert.match(translation, /do not generate a summary artifact/i);
  assert.match(daily, /subscription-report-export --period daily --to zh-CN/);
  assert.match(daily, /current San Francisco calendar date/);
  assert.match(weekly, /subscription-report-export --period weekly --to zh-CN/);
  assert.match(weekly, /midnight on Monday/);
  assert.equal(weeklyUrl.searchParams.get("path"), "/workspace");
});

test("public builds omit a machine-specific workspace path", () => {
  const url = new URL(articleCodexLink(9, "summary", ""));

  assert.equal(CODEX_WORKSPACE_PATH, process.env.NEXT_PUBLIC_CODEX_WORKSPACE_PATH?.trim() ?? "");
  assert.equal(url.searchParams.has("path"), false);
  assert.doesNotMatch(url.href, /\/Users\/|smzdm|chatgpt\.site/);
});
