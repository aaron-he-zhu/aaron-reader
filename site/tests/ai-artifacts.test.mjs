import assert from "node:assert/strict";
import test from "node:test";

import {
  artifactPayload,
  artifactSearchText,
  findArtifact,
} from "../app/ai-artifacts.ts";

const artifacts = [
  {
    id: 11,
    task: "summary",
    input_scope: "metadata",
    target_language: "zh-CN",
    generated_at: "2026-08-01T01:00:00Z",
    output: {
      summary: "中文 AI 摘要",
      key_points: ["第一点", "第二点"],
      language: "zh-CN",
      basis: "metadata",
      limitations: "",
    },
  },
  {
    id: 12,
    task: "translation",
    input_scope: "metadata",
    target_language: "zh-CN",
    generated_at: "2026-08-01T01:01:00Z",
    output: {
      title: "标题译文",
      publisher_summary: "发布方简介译文",
      language: "zh-CN",
      limitations: "",
    },
  },
];

test("reads the nested cached artifact shape", () => {
  const translation = findArtifact(artifacts, "translation", "zh-CN");
  const summary = findArtifact(artifacts, "summary", "zh-CN");

  assert.deepEqual(artifactPayload(translation), {
    title: "标题译文",
    publisherSummary: "发布方简介译文",
    summary: null,
    keyPoints: [],
  });
  assert.deepEqual(artifactPayload(summary), {
    title: null,
    publisherSummary: null,
    summary: "中文 AI 摘要",
    keyPoints: ["第一点", "第二点"],
  });
  assert.equal(findArtifact(artifacts, "translation", "en"), undefined);
});

test("keeps AI artifact content searchable", () => {
  assert.deepEqual(artifactSearchText(artifacts), [
    "中文 AI 摘要",
    "第一点",
    "第二点",
    "标题译文",
    "发布方简介译文",
  ]);
});

test("keeps compatibility with an older flattened artifact", () => {
  const legacy = {
    task: "summary",
    language: "en",
    output: "Legacy cached summary",
    key_points: ["Legacy point", ""],
  };

  assert.deepEqual(artifactPayload(legacy), {
    title: null,
    publisherSummary: null,
    summary: "Legacy cached summary",
    keyPoints: ["Legacy point"],
  });
});
