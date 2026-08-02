import assert from "node:assert/strict";
import test from "node:test";

import { findLatestReport, reportPayload } from "../app/ai-reports.ts";

const reports = [
  {
    id: 1,
    period: "daily",
    target_language: "zh-CN",
    period_end: "2026-08-01T08:00:00Z",
    output: { headline: "旧日报", overview: "旧内容", items: [], language: "zh-CN" },
  },
  {
    id: 2,
    period: "daily",
    target_language: "zh-CN",
    generated_at: "2026-08-01T10:00:00Z",
    output: {
      headline: "今日 AI 动态",
      overview: "两项值得关注的更新。",
      language: "zh-CN",
      limitations: "仅依据发布方元数据。",
      items: [
        { article_id: 110, title: "第一篇", summary: "第一篇摘要" },
        { article_id: 111, title: "", summary: "无标题会被忽略" },
      ],
    },
  },
  {
    id: 3,
    period: "weekly",
    target_language: "en",
    generated_at: "2026-08-01T11:00:00Z",
    output: { headline: "Weekly", overview: "English fallback", items: [], language: "en" },
  },
];

test("selects the latest cached report for a period and preferred language", () => {
  assert.equal(findLatestReport(reports, "daily", "zh-CN")?.id, 2);
  assert.equal(findLatestReport(reports, "weekly", "zh-CN")?.id, 3);
  assert.equal(findLatestReport(undefined, "daily"), undefined);
});

test("normalizes report output for rendering", () => {
  const payload = reportPayload(reports[1]);
  assert.deepEqual(payload, {
    headline: "今日 AI 动态",
    overview: "两项值得关注的更新。",
    limitations: "仅依据发布方元数据。",
    items: [{ articleId: 110, title: "第一篇", summary: "第一篇摘要" }],
  });
});
