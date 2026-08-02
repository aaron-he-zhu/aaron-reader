import type { ReportPeriod } from "./codex-actions";

export type AIReportItem = {
  article_id?: number;
  title?: string;
  summary?: string;
};

export type AIReportOutput = {
  headline?: string;
  overview?: string;
  items?: AIReportItem[];
  language?: string;
  limitations?: string;
};

export type AIReport = {
  id?: number;
  period?: string;
  timezone?: string;
  local_date?: string;
  period_start?: string;
  period_end?: string;
  target_language?: string;
  generated_at?: string | null;
  input_truncated?: boolean;
  provider?: string;
  model?: string;
  output?: AIReportOutput;
};

export type ReportPayload = {
  headline: string | null;
  overview: string | null;
  items: Array<{ articleId: number; title: string; summary: string }>;
  limitations: string | null;
};

function cleanText(value: unknown) {
  if (typeof value !== "string") return null;
  const cleaned = value.trim();
  return cleaned || null;
}

function timestamp(report: AIReport) {
  for (const value of [report.generated_at, report.period_end, report.period_start]) {
    if (!value) continue;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function hasLanguage(report: AIReport, language: string) {
  const targetLanguage = cleanText(report.target_language);
  const outputLanguage = cleanText(report.output?.language);
  if (targetLanguage && outputLanguage && targetLanguage !== outputLanguage) return false;
  return (targetLanguage || outputLanguage) === language;
}

export function findLatestReport(
  reports: AIReport[] | undefined,
  period: ReportPeriod,
  language: string,
) {
  return (reports || []).filter(
    (report) => report.period === period && hasLanguage(report, language),
  )
    .sort((left, right) => timestamp(right) - timestamp(left))[0];
}

export function reportPayload(report: AIReport | undefined): ReportPayload {
  const items = Array.isArray(report?.output?.items)
    ? report.output.items.flatMap((item) => {
        const articleId = item?.article_id;
        const title = cleanText(item?.title);
        const summary = cleanText(item?.summary);
        if (!Number.isInteger(articleId) || !title || !summary) return [];
        return [{ articleId: articleId as number, title, summary }];
      })
    : [];

  return {
    headline: cleanText(report?.output?.headline),
    overview: cleanText(report?.output?.overview),
    items,
    limitations: cleanText(report?.output?.limitations),
  };
}
