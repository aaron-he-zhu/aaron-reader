import assert from "node:assert/strict";
import test from "node:test";

import {
  artifactPayload,
  findArtifact,
  hasValidTranslation,
  textAppearsTranslated,
  translationSearchText,
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

test("keeps translated article metadata searchable across interface languages", () => {
  assert.deepEqual(translationSearchText(artifacts), [
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

test("textAppearsTranslated returns false for Latin-only zh-CN output", () => {
  assert.equal(textAppearsTranslated("This is English only", "zh-CN"), false);
  assert.equal(textAppearsTranslated("Some long Latin text without any Chinese characters", "zh-CN"), false);
});

test("textAppearsTranslated returns true for text with CJK characters", () => {
  assert.equal(textAppearsTranslated("中文标题", "zh-CN"), true);
  assert.equal(textAppearsTranslated("GPT-5 发布公告", "zh-CN"), true);
  assert.equal(textAppearsTranslated("OpenAI 的新模型", "zh-CN"), true);
});

test("textAppearsTranslated returns true for short text", () => {
  assert.equal(textAppearsTranslated("Short", "zh-CN"), true);
  assert.equal(textAppearsTranslated(null, "zh-CN"), true);
  assert.equal(textAppearsTranslated("", "zh-CN"), true);
});

test("textAppearsTranslated returns true for English target language", () => {
  assert.equal(textAppearsTranslated("This is English only", "en"), true);
});

test("hasValidTranslation returns false for Latin-only zh-CN translation", () => {
  const payload = {
    title: "English Title Without Translation",
    publisherSummary: "English summary without translation",
    summary: null,
    keyPoints: [],
  };
  assert.equal(hasValidTranslation(payload, "zh-CN"), false);
});

test("hasValidTranslation returns true for properly translated zh-CN", () => {
  const payload = {
    title: "中文标题",
    publisherSummary: "中文简介",
    summary: null,
    keyPoints: [],
  };
  assert.equal(hasValidTranslation(payload, "zh-CN"), true);
});

test("hasValidTranslation returns false for empty translation", () => {
  const payload = {
    title: null,
    publisherSummary: null,
    summary: null,
    keyPoints: [],
  };
  assert.equal(hasValidTranslation(payload, "zh-CN"), false);
});

test("hasValidTranslation returns true when title is translated but summary is Latin", () => {
  const payload = {
    title: "中文标题",
    publisherSummary: null,
    summary: null,
    keyPoints: [],
  };
  assert.equal(hasValidTranslation(payload, "zh-CN"), true);
});
