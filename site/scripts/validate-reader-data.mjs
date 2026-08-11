import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const siteRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dataSnapshotPath = resolve(siteRoot, "data", "latest.json");
const publicSnapshotPath = resolve(siteRoot, "public", "reader", "latest.json");
const feedPath = resolve(siteRoot, "public", "reader", "feed.xml");
const digestPath = resolve(siteRoot, "public", "reader", "digest.md");

const [dataBytes, publicBytes, feed, digest] = await Promise.all([
  readFile(dataSnapshotPath),
  readFile(publicSnapshotPath),
  readFile(feedPath, "utf8"),
  readFile(digestPath, "utf8"),
]);

if (!dataBytes.equals(publicBytes)) {
  throw new Error("The imported data and public reader snapshots must be identical.");
}

const snapshot = JSON.parse(dataBytes.toString("utf8"));
if (!Array.isArray(snapshot.articles) || !Array.isArray(snapshot.sources)) {
  throw new Error("The reader snapshot is missing article or source arrays.");
}
if (snapshot.articles.some(
  (article) => !article || typeof article !== "object" || !Array.isArray(article.ai_artifacts),
)) {
  throw new Error("Every public article must contain an AI artifact array.");
}
if (snapshot.llm_tokens_used !== 0 || snapshot.render_llm_tokens_used !== 0) {
  throw new Error("Deterministic collection or rendering unexpectedly used LLM tokens.");
}
if ("unread" in (snapshot.counts || {}) || "starred" in (snapshot.counts || {})) {
  throw new Error("The public snapshot must not expose private read or star counts.");
}
if (snapshot.articles.some((article) => "unread" in article || "starred" in article)) {
  throw new Error("The public snapshot must not expose private article state.");
}
if (snapshot.articles.some((article) => (article.ai_artifacts || []).some(
  (artifact) => "provider" in artifact || "model" in artifact,
))) {
  throw new Error("The public snapshot must not expose AI provider provenance.");
}
const articleArtifacts = snapshot.articles.flatMap((article) => article.ai_artifacts);
if (articleArtifacts.some(
  (artifact) => !artifact || typeof artifact !== "object" || artifact.task !== "translation",
)) {
  throw new Error("The public snapshot may expose only per-article translations.");
}
if (snapshot.cached_ai_artifact_count !== articleArtifacts.length) {
  throw new Error("The cached article translation count is inconsistent.");
}
if ("ai_reports" in snapshot || "cached_ai_report_count" in snapshot) {
  throw new Error("The public snapshot must not contain removed AI brief fields.");
}
if (snapshot.sources.some(
  (source) => "unread_count" in source || "pending_count" in source || "last_error" in source,
)) {
  throw new Error("The public snapshot must not expose private source state or errors.");
}

const combined = `${dataBytes.toString("utf8")}\n${feed}\n${digest}`;
if (/\/Users\/[^/]+\//.test(combined) || /[A-Za-z]:\\Users\\[^\\]+\\/.test(combined)) {
  throw new Error("Public reader artifacts contain a machine-specific user path.");
}
if (!/^\s*<\?xml\b[\s\S]*<rss\b/i.test(feed) || !digest.trim()) {
  throw new Error("The public RSS feed or Markdown digest is invalid.");
}
if (!digest.startsWith("# Aaron Reader Public Digest\n")) {
  throw new Error("The Markdown digest must be generated from the public article list.");
}
if (/Aaron Reader Unread Digest|\bunread articles\b|Aaron Reader 未读摘要/i.test(digest)) {
  throw new Error("The public Markdown digest must not disclose private unread state.");
}

console.log(
  `Validated ${snapshot.articles.length} public articles and ${snapshot.sources.length} sources.`,
);
