"use client";

import { useEffect, useMemo, useState } from "react";

import {
  artifactPayload,
  findArtifact,
  hasValidTranslation,
  translationSearchText,
  type AIArtifact,
} from "./ai-artifacts";

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
  sources: Source[];
  articles: Article[];
};

const copy = {
  en: {
    title: "Aaron Reader",
    intro:
      "Official OpenAI and Anthropic posts, in English and 简体中文. We only translate the title and short blurb — click through to read the original.",
    updated: "Updated",
    articles: "articles",
    sources: "sources",
    search: "Search articles",
    allSources: "All sources",
    showing: "Showing",
    of: "of",
    noResults: "No articles match this view.",
    clear: "Clear filters",
    more: "Show more",
    about: "About",
    aboutText: "Updated twice a day. Chinese is added automatically for title + blurb.",
    language: "Language",
    sourceHealth: "Source health",
    healthy: "Healthy",
    degraded: "Degraded",
    error: "Error",
    never_synced: "Awaiting first sync",
    feed: "RSS feed",
    digest: "Markdown digest",
    notTranslatedYet: "Chinese not ready yet — showing English.",
    noSummary: "No short summary from the publisher.",
  },
  "zh-CN": {
    title: "Aaron Reader",
    intro:
      "把 OpenAI 和 Anthropic 的官方文章收成一份列表，中英都能看。中文只翻译标题和简介，点进去读原文。",
    updated: "更新时间",
    articles: "篇文章",
    sources: "个来源",
    search: "搜索文章",
    allSources: "全部来源",
    showing: "当前显示",
    of: "共",
    noResults: "没有符合当前条件的文章。",
    clear: "清除筛选",
    more: "显示更多",
    about: "关于",
    aboutText: "每天更新两次。中文只自动补标题和简介。",
    language: "语言",
    sourceHealth: "来源状态",
    healthy: "正常",
    degraded: "降级",
    error: "错误",
    never_synced: "等待首次同步",
    feed: "RSS 订阅",
    digest: "Markdown 摘要",
    notTranslatedYet: "中文还没好，先显示英文。",
    noSummary: "发布方没有提供简介。",
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
  const timeZone = language === "zh-CN" ? "Asia/Shanghai" : "America/Los_Angeles";
  return new Intl.DateTimeFormat(language, long
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone, timeZoneName: "short" }
    : { year: "numeric", month: "short", day: "numeric", timeZone }
  ).format(date);
}

export function Reader({ snapshot }: { snapshot: Snapshot }) {
  const [language, setLanguage] = useState<Language>("en");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [visible, setVisible] = useState(50);
  const t = copy[language];

  useEffect(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlLang = urlParams.get("lang");
    const stored = window.localStorage.getItem("aaron-reader-language");
    const frame = window.requestAnimationFrame(() => {
      if (urlLang === "zh-CN" || urlLang === "en") {
        setLanguage(urlLang);
      } else if (stored === "zh-CN" || stored === "en") {
        setLanguage(stored);
      } else {
        const browserLang = navigator.language || "";
        if (browserLang.startsWith("zh")) {
          setLanguage("zh-CN");
        }
      }
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
            <span className="lang-sep" aria-hidden="true">/</span>
            <button className={language === "zh-CN" ? "active" : ""} onClick={() => chooseLanguage("zh-CN")}>简</button>
          </span>
        </nav>
      </header>

      <div className="page" id="top">
        <section className="hero" aria-labelledby="page-title">
          <div>
            <h1 id="page-title">{t.title}</h1>
          </div>
          <div className="hero-copy">
            <p>{t.intro}</p>
            <p className="timestamp">{t.updated} · {formatDate(snapshot.generated_at, language, true)}</p>
          </div>
        </section>

        <section className="metrics" aria-label="Reader metrics">
          <div><strong>{snapshot.articles.length}</strong><span>{t.articles}</span></div>
          <div><strong>{snapshot.sources.length}</strong><span>{t.sources}</span></div>
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
                    setVisible(50);
                  }}
                  placeholder={t.search}
                  aria-label={t.search}
                />
              </label>
              <select value={source} onChange={(event) => {
                setSource(event.target.value);
                setVisible(50);
              }} aria-label={t.allSources}>
                <option value="">{t.allSources}</option>
                {snapshot.sources.map((item) => <option key={item.slug} value={item.slug}>{item.name}</option>)}
              </select>
            </div>

            <div className="stream-heading">
              <p>{t.showing} <strong>{Math.min(filtered.length, visible)}</strong> {t.of} {filtered.length}</p>
              {(query || source) && <button onClick={reset}>{t.clear}</button>}
            </div>

            <div className="article-list">
              {filtered.slice(0, visible).map((article, index) => {
                const translationArtifact = findArtifact(article.ai_artifacts, "translation", language);
                const translation = artifactPayload(translationArtifact);
                const hasTranslation = hasValidTranslation(translation, language);
                const title = hasTranslation && translation.title ? translation.title : article.title;
                const publisherSummary = hasTranslation && translation.publisherSummary ? translation.publisherSummary : article.summary;
                const showNotTranslatedYet = language === "zh-CN" && !hasTranslation;
                const hasSummary = Boolean(publisherSummary);
                return (
                  <article className="article" key={article.id} style={{ "--source-color": sourceColor[article.source] || "#171717" } as React.CSSProperties}>
                    <span className="article-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
                    <div className="article-body">
                      <div className="article-meta">
                        <span className="source-name"><i />{article.source_name}</span>
                        <time dateTime={article.published_at || undefined}>{formatDate(article.published_at, language)}</time>
                        {article.category && <span>{article.category}</span>}
                      </div>
                      <h2><a href={article.url} target="_blank" rel="noopener noreferrer">{title}<span className="external-arrow" aria-hidden="true">↗</span></a></h2>
                      {showNotTranslatedYet && (
                        <p className="not-translated-note">{t.notTranslatedYet}</p>
                      )}
                      {hasSummary ? <p>{publisherSummary}</p> : <p className="no-summary">{t.noSummary}</p>}
                    </div>
                  </article>
                );
              })}
            </div>

            {filtered.length === 0 && <div className="empty"><p>{t.noResults}</p><button onClick={reset}>{t.clear}</button></div>}
            {visible < filtered.length && <button className="show-more" onClick={() => setVisible((count) => count + 50)}>{t.more} <span>+{Math.min(50, filtered.length - visible)}</span></button>}
          </div>

          <aside className="system-panel">
            <p className="panel-kicker">{t.about}</p>
            <p className="about-text">{t.aboutText}</p>
            <div className="health-block">
              <p>{t.sourceHealth}</p>
              {snapshot.sources.map((item) => (
                <div className="health-row" key={item.slug}>
                  <span>{item.name}</span>
                  <span className={`health-value ${item.health}`}><i />{t[item.health as keyof typeof t] || item.health}</span>
                </div>
              ))}
            </div>
          </aside>
        </section>
      </div>

      <footer><span>Aaron Reader · 2026</span></footer>
    </main>
  );
}
