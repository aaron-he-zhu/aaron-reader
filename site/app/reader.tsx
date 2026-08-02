"use client";

import { useEffect, useMemo, useState } from "react";

import {
  artifactPayload,
  findArtifact,
  translationSearchText,
  type AIArtifact,
} from "./ai-artifacts";
import {
  findLatestReport,
  reportPayload,
  type AIReport,
  type ReportPeriod,
} from "./ai-reports";

type Language = "en" | "zh-CN";

type Article = {
  id: number;
  source: string;
  source_name: string;
  title: string;
  summary: string | null;
  category: string | null;
  published_at: string | null;
  url: string;
  ai_artifacts: AIArtifact[];
};

type Source = {
  slug: string;
  name: string;
  health: string;
  last_success_at: string | null;
  article_count: number;
};

type Snapshot = {
  generated_at: string;
  language: string;
  supported_languages: string[];
  llm_tokens_used: number;
  render_llm_tokens_used: number;
  cached_ai_artifact_count: number;
  ai_reports?: AIReport[];
  sources: Source[];
  articles: Article[];
};

const copy = {
  en: {
    eyebrow: "Independent signal desk",
    titleA: "The work behind",
    titleB: "the AI frontier.",
    intro:
      "OpenAI, Anthropic, and Claude updates in one calm, deterministic feed. Collection, parsing, and publishing use fixed programs—not an LLM.",
    updated: "Updated",
    articles: "articles",
    sources: "sources",
    zero: "LLM tokens for collection",
    search: "Search the archive",
    allSources: "All sources",
    showing: "Showing",
    of: "of",
    noResults: "No articles match this view.",
    clear: "Clear filters",
    read: "Read original",
    more: "Show more",
    system: "System",
    collection: "Collection",
    deterministic: "GitHub Actions · 09:15 & 21:15 San Francisco",
    enrichment: "AI enrichment",
    enrichmentValue: "Default · OpenRouter Free → DeepSeek V4 Flash",
    language: "Language",
    sourceHealth: "Source health",
    healthy: "Healthy",
    degraded: "Degraded",
    error: "Error",
    never_synced: "Awaiting first sync",
    cached: "cached article translations",
    aiTranslation: "Publisher metadata translation",
    generatedCached: "AI-generated · cached",
    generatedOn: "Generated",
    inputTruncated: "Input truncated",
    feed: "RSS feed",
    digest: "Markdown digest",
    coverage: "Chinese AI automation coverage",
    coverageSuffix: "Cloud automation will fill missing article translations on its next run.",
    coverageCompleteSuffix: "All articles have a cached Chinese translation.",
    reports: "AI briefs",
    dailyReport: "Daily brief",
    weeklyReport: "Weekly brief",
    reportTimezone: "San Francisco time",
    reportItems: "Source notes",
    noReports: "No daily or weekly AI brief has been published yet.",
    reportLimitations: "Scope note",
    policy:
      "By default, each scheduled cloud run starts with the fixed OpenRouter Free profile and can switch once to DeepSeek V4 Flash only when the OpenRouter credential is missing before transmission, after an explicit OpenRouter 401, 402, 404, or 429 response, or after a closed, non-policy availability/profile failure or locally invalid result with fully audited usage. An explicitly selected DeepSeek-only run receives no OpenRouter credential and never falls back in reverse. Ambiguous network results, unknown or future error codes, and safety or policy refusals never fall back. The active profile refreshes cached Chinese article translations and the daily brief twice daily; the weekly brief is generated once on Sunday evening. OpenRouter Free may resolve to and internally route among different eligible free providers; Aaron Reader's one-way rule covers only its separate DeepSeek continuation. The browser only reads the latest verified snapshot and never calls a model or AI API.",
    footer: "Built for signal, not engagement.",
  },
  "zh-CN": {
    eyebrow: "独立信号台",
    titleA: "读懂 AI 前沿",
    titleB: "背后的工作。",
    intro:
      "把 OpenAI、Anthropic 与 Claude 的更新汇集成一份安静、确定性的订阅。采集、解析和发布均由固定程序完成，不调用 LLM。",
    updated: "更新时间",
    articles: "篇文章",
    sources: "个来源",
    zero: "采集所用 LLM token",
    search: "搜索全部文章",
    allSources: "全部来源",
    showing: "当前显示",
    of: "共",
    noResults: "没有符合当前条件的文章。",
    clear: "清除筛选",
    read: "阅读原文",
    more: "显示更多",
    system: "系统",
    collection: "订阅采集",
    deterministic: "GitHub Actions · 旧金山时间 09:15、21:15",
    enrichment: "AI 增强",
    enrichmentValue: "默认 · OpenRouter Free → DeepSeek V4 Flash",
    language: "语言",
    sourceHealth: "来源状态",
    healthy: "正常",
    degraded: "降级",
    error: "错误",
    never_synced: "等待首次同步",
    cached: "个已缓存文章翻译",
    aiTranslation: "发布方元数据翻译",
    generatedCached: "AI 生成 · 已缓存",
    generatedOn: "生成于",
    inputTruncated: "输入已截断",
    feed: "RSS 订阅",
    digest: "Markdown 摘要",
    coverage: "中文 AI 自动覆盖",
    coverageSuffix: "缺失的文章翻译会由云端自动化在下一轮补齐。",
    coverageCompleteSuffix: "全部文章都已有缓存的中文翻译。",
    reports: "AI 简报",
    dailyReport: "今日简报",
    weeklyReport: "本周简报",
    reportTimezone: "旧金山时间",
    reportItems: "本期文章",
    noReports: "目前还没有已发布的日报或周报。",
    reportLimitations: "范围说明",
    policy: "每轮定时云端任务默认从固定的 OpenRouter Free profile 开始；只有发送前缺少 OpenRouter 凭据、OpenRouter 明确返回 401、402、404 或 429，或封闭白名单内的非政策可用性/profile 失败或本地无效结果已完整落账时，才会单向切换一次到 DeepSeek V4 Flash。显式选择的 DeepSeek-only 任务不会接收 OpenRouter 凭据，也绝不会反向兜底。网络结果不明、用量未知、未来未知错误码以及安全或政策拒答绝不会兜底。当前 profile 每天两次更新缓存的中文文章翻译和日报；周报仅在周日晚上生成一次。OpenRouter Free 每次请求都可能解析到不同的合格免费模型，也可能在内部上游间路由；Aaron Reader 的单向规则只约束它自己后续发起的 DeepSeek 调用。浏览器只读取最新的已验证快照，不会调用模型或 AI API。",
    footer: "为信号而建，而非为互动量而建。",
  },
} as const;

const sourceColor: Record<string, string> = {
  "openai-news": "#0d8a6a",
  "openai-developers": "#2463d4",
  "claude-blog": "#c25f3c",
  "anthropic-news": "#7047b8",
};

function formatDate(value: string | null, language: Language, long = false) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat(language, long
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Singapore", timeZoneName: "short" }
    : { year: "numeric", month: "short", day: "numeric", timeZone: "Asia/Singapore" }
  ).format(date);
}

function formatReportWindow(report: AIReport, language: Language) {
  const start = report.period_start;
  const end = report.period_end;
  if (!start || !end) return report.local_date || "—";

  const dateFormatter = new Intl.DateTimeFormat(language, {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "America/Los_Angeles",
  });
  const cutoffFormatter = new Intl.DateTimeFormat(language, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "America/Los_Angeles",
    timeZoneName: "short",
  });
  return `${dateFormatter.format(new Date(start))} → ${cutoffFormatter.format(new Date(end))}`;
}

export function Reader({ snapshot }: { snapshot: Snapshot }) {
  const [language, setLanguage] = useState<Language>("en");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [visible, setVisible] = useState(36);
  const t = copy[language];
  const reportPeriods: ReportPeriod[] = ["daily", "weekly"];

  useEffect(() => {
    const stored = window.localStorage.getItem("aaron-reader-language");
    const frame = window.requestAnimationFrame(() => {
      if (stored === "zh-CN" || stored === "en") setLanguage(stored);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    document.documentElement.lang = language;
    window.localStorage.setItem("aaron-reader-language", language);
  }, [language]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase(language);
    return snapshot.articles.filter((article) => {
      if (source && article.source !== source) return false;
      if (!needle) return true;
      const artifact = findArtifact(article.ai_artifacts, "translation", language);
      const translated = artifactPayload(artifact);
      const haystack = [
        article.title,
        article.summary,
        article.category,
        article.source_name,
        translated.title,
        translated.publisherSummary,
        ...translationSearchText(article.ai_artifacts),
      ].filter(Boolean).join(" ").toLocaleLowerCase(language);
      return haystack.includes(needle);
    });
  }, [language, query, snapshot.articles, source]);

  const articlesById = useMemo(
    () => new Map(snapshot.articles.map((article) => [article.id, article])),
    [snapshot.articles],
  );
  const cachedTranslationArtifactCount = useMemo(
    () => snapshot.articles.reduce(
      (count, article) => count + article.ai_artifacts.filter(
        (artifact) => artifact.task === "translation",
      ).length,
      0,
    ),
    [snapshot.articles],
  );
  const chineseTranslatedArticleCount = useMemo(
    () => snapshot.articles.filter(
      (article) => Boolean(findArtifact(article.ai_artifacts, "translation", "zh-CN")),
    ).length,
    [snapshot.articles],
  );
  const translationCoverageComplete = (
    snapshot.articles.length > 0
    && chineseTranslatedArticleCount === snapshot.articles.length
  );
  const publishedReports = reportPeriods.flatMap((period) => {
    const report = findLatestReport(snapshot.ai_reports, period, language);
    return report ? [{ period, report, payload: reportPayload(report) }] : [];
  });

  const chooseLanguage = (next: Language) => {
    setLanguage(next);
  };

  const reset = () => {
    setQuery("");
    setSource("");
  };

  return (
    <main>
      <header className="masthead">
        <a className="wordmark" href="#top" aria-label="Aaron Reader home">
          <span className="wordmark-mark" aria-hidden="true">A</span>
          <span>Aaron Reader</span>
        </a>
        <nav className="topnav" aria-label={t.language}>
          <a href="/reader/feed.xml">{t.feed}</a>
          <a href="/reader/digest.md">{t.digest}</a>
          <span className="language-switch" role="group" aria-label={t.language}>
            <button className={language === "en" ? "active" : ""} onClick={() => chooseLanguage("en")}>EN</button>
            <span aria-hidden="true">/</span>
            <button className={language === "zh-CN" ? "active" : ""} onClick={() => chooseLanguage("zh-CN")}>简</button>
          </span>
        </nav>
      </header>

      <div className="page" id="top">
        <section className="hero" aria-labelledby="page-title">
          <div>
            <p className="eyebrow"><span className="live-dot" />{t.eyebrow}</p>
            <h1 id="page-title"><span>{t.titleA}</span> {t.titleB}</h1>
          </div>
          <div className="hero-copy">
            <p>{t.intro}</p>
            <p className="timestamp">{t.updated} · {formatDate(snapshot.generated_at, language, true)}</p>
          </div>
        </section>

        <section className="metrics" aria-label="Reader metrics">
          <div><strong>{snapshot.articles.length}</strong><span>{t.articles}</span></div>
          <div><strong>{snapshot.sources.length}</strong><span>{t.sources}</span></div>
          <div><strong>{snapshot.render_llm_tokens_used}</strong><span>{t.zero}</span></div>
        </section>

        <section className="workspace">
          <div className="stream">
            <div className="filters">
              <label className="search-field">
                <span aria-hidden="true">⌕</span>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    setVisible(36);
                  }}
                  placeholder={t.search}
                  aria-label={t.search}
                />
              </label>
              <select value={source} onChange={(event) => {
                setSource(event.target.value);
                setVisible(36);
              }} aria-label={t.allSources}>
                <option value="">{t.allSources}</option>
                {snapshot.sources.map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}
              </select>
            </div>

            {publishedReports.length > 0 && (
              <section className="report-section" aria-labelledby="report-section-title">
                <div className="report-section-heading">
                  <h2 className="panel-kicker" id="report-section-title">{t.reports}</h2>
                  <span>{t.reportTimezone}</span>
                </div>
                <div className="report-grid">
                  {publishedReports.map(({ period, report, payload }) => (
                    <article
                      className="report-card"
                      key={`${period}-${report.id || report.generated_at || report.period_end}`}
                      lang={report.target_language || report.output?.language || language}
                    >
                      <div className="report-card-meta">
                        <strong>{period === "daily" ? t.dailyReport : t.weeklyReport}</strong>
                        <span className="report-ai-status"><i />{t.generatedCached}</span>
                      </div>
                      <p className="report-window">{formatReportWindow(report, language)}</p>
                      {payload.headline && <h3>{payload.headline}</h3>}
                      {payload.overview && <p className="report-overview">{payload.overview}</p>}
                      {payload.items.length > 0 && (
                        <details className="report-items">
                          <summary>
                            <span>{t.reportItems}</span>
                            <strong>{payload.items.length}</strong>
                            <span className="report-disclosure" aria-hidden="true">+</span>
                          </summary>
                          <ol>
                            {payload.items.map((item) => {
                              const article = articlesById.get(item.articleId);
                              return (
                                <li key={`${report.id || period}-${item.articleId}`}>
                                  {article ? (
                                    <a href={article.url} target="_blank" rel="noopener noreferrer">{item.title}</a>
                                  ) : <strong>{item.title}</strong>}
                                  <p>{item.summary}</p>
                                </li>
                              );
                            })}
                          </ol>
                          {payload.limitations && (
                            <p className="report-limitations"><strong>{t.reportLimitations}:</strong> {payload.limitations}</p>
                          )}
                        </details>
                      )}
                      <div className="report-footer">
                        {report.generated_at && (
                          <time dateTime={report.generated_at}>{t.generatedOn} {formatDate(report.generated_at, language)}</time>
                        )}
                        {report.input_truncated && <span className="ai-warning">{t.inputTruncated}</span>}
                      </div>
                    </article>
                  ))}
                </div>
              </section>
            )}

            <div className="stream-heading">
              <p>{t.showing} <strong>{Math.min(filtered.length, visible)}</strong> {t.of} {filtered.length}</p>
              {(query || source) && <button onClick={reset}>{t.clear}</button>}
            </div>

            <div className="article-list">
              {filtered.slice(0, visible).map((article, index) => {
                const translationArtifact = findArtifact(article.ai_artifacts, "translation", language);
                const translation = artifactPayload(translationArtifact);
                const title = translation.title || article.title;
                const publisherSummary = translation.publisherSummary || article.summary;
                const hasTranslation = Boolean(translation.title || translation.publisherSummary);
                return (
                  <article className="article" key={article.id} style={{ "--source-color": sourceColor[article.source] || "#171717" } as React.CSSProperties}>
                    <span className="article-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                    <div className="article-body">
                      <div className="article-meta">
                        <span className="source-name"><i />{article.source_name}</span>
                        <time dateTime={article.published_at || undefined}>{formatDate(article.published_at, language)}</time>
                        {article.category && <span>{article.category}</span>}
                      </div>
                      <h2><a href={article.url} target="_blank" rel="noopener noreferrer">{title}</a></h2>
                      {hasTranslation && (
                        <div className="ai-translation-note" aria-label={t.aiTranslation}>
                          <strong>{t.aiTranslation}</strong>
                          <span className="ai-state"><i />{t.generatedCached}</span>
                          {translationArtifact?.generated_at && (
                            <time dateTime={translationArtifact.generated_at}>
                              {t.generatedOn} {formatDate(translationArtifact.generated_at, language)}
                            </time>
                          )}
                        </div>
                      )}
                      {publisherSummary && <p>{publisherSummary}</p>}
                      <div className="article-links">
                        <a className="read-link" href={article.url} target="_blank" rel="noopener noreferrer">{t.read}<span aria-hidden="true">↗</span></a>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>

            {filtered.length === 0 && <div className="empty"><p>{t.noResults}</p><button onClick={reset}>{t.clear}</button></div>}
            {visible < filtered.length && <button className="show-more" onClick={() => setVisible((count) => count + 36)}>{t.more} <span>+{Math.min(36, filtered.length - visible)}</span></button>}
          </div>

          <aside className="system-panel">
            <p className="panel-kicker">{t.system}</p>
            <dl className="system-list">
              <div><dt>{t.collection}</dt><dd><span className="status-dot healthy" />{t.deterministic}</dd></div>
              <div><dt>{t.enrichment}</dt><dd>{t.enrichmentValue}</dd></div>
              <div>
                <dt>{t.coverage}</dt>
                <dd className="coverage-value">
                  <strong>{chineseTranslatedArticleCount}/{snapshot.articles.length}</strong>
                  <span>{translationCoverageComplete ? t.coverageCompleteSuffix : t.coverageSuffix}</span>
                </dd>
              </div>
              <div><dt>{t.language}</dt><dd>English / 简体中文</dd></div>
            </dl>
            <p className="policy">{t.policy}</p>
            <div className="health-block">
              <p>{t.sourceHealth}</p>
              {snapshot.sources.map((item) => (
                <div className="health-row" key={item.slug}>
                  <span>{item.name}</span>
                  <span className={`health-value ${item.health}`}><i />{t[item.health as keyof typeof t] || item.health}</span>
                </div>
              ))}
            </div>
            <p className="cache-note">{cachedTranslationArtifactCount} {t.cached}</p>
          </aside>
        </section>
      </div>

      <footer><span>Aaron Reader · 2026</span><span>{t.footer}</span></footer>
    </main>
  );
}
