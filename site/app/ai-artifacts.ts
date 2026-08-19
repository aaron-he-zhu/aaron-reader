export type AIArtifactOutput = {
  summary?: string | null;
  key_points?: unknown;
  title?: string | null;
  publisher_summary?: string | null;
  language?: string;
  basis?: string;
  limitations?: string;
};

export type AIArtifact = {
  id?: number;
  task?: string;
  input_scope?: string;
  target_language?: string;
  generated_at?: string | null;
  input_truncated?: boolean;
  output?: AIArtifactOutput | string;

  // These fields keep older, flattened snapshots readable.
  language?: string;
  title?: string | null;
  summary?: string | null;
  key_points?: unknown;
};

export type ArtifactPayload = {
  title: string | null;
  publisherSummary: string | null;
  summary: string | null;
  keyPoints: string[];
};

function cleanText(value: unknown) {
  if (typeof value !== "string") return null;
  const cleaned = value.trim();
  return cleaned || null;
}

function objectOutput(artifact: AIArtifact) {
  return artifact.output && typeof artifact.output === "object"
    ? artifact.output
    : undefined;
}

export function artifactLanguage(artifact: AIArtifact) {
  const output = objectOutput(artifact);
  return cleanText(artifact.target_language)
    || cleanText(artifact.language)
    || cleanText(output?.language);
}

export function findArtifact(
  artifacts: AIArtifact[] | undefined,
  task: "summary" | "translation",
  language: string,
) {
  return artifacts?.find(
    (artifact) => artifact.task === task && artifactLanguage(artifact) === language,
  );
}

export function artifactPayload(artifact: AIArtifact | undefined): ArtifactPayload {
  if (!artifact) {
    return { title: null, publisherSummary: null, summary: null, keyPoints: [] };
  }

  const output = objectOutput(artifact);
  const legacyOutput = typeof artifact.output === "string" ? cleanText(artifact.output) : null;
  const rawPoints = output?.key_points ?? artifact.key_points;
  const keyPoints = Array.isArray(rawPoints)
    ? rawPoints.map(cleanText).filter((point): point is string => point !== null)
    : [];

  return {
    title: cleanText(output?.title) || cleanText(artifact.title),
    publisherSummary:
      cleanText(output?.publisher_summary)
      || (artifact.task === "translation" ? cleanText(output?.summary) : null)
      || (artifact.task === "translation" ? cleanText(artifact.summary) : null),
    summary:
      cleanText(output?.summary)
      || (artifact.task === "summary" ? cleanText(artifact.summary) : null)
      || (artifact.task === "summary" ? legacyOutput : null),
    keyPoints,
  };
}

export function translationSearchText(artifacts: AIArtifact[] | undefined) {
  return (artifacts || [])
    .filter((artifact) => artifact.task === "translation")
    .flatMap((artifact) => {
      const payload = artifactPayload(artifact);
      return [payload.title, payload.publisherSummary]
        .filter((value): value is string => value !== null);
    });
}

function hasCjk(text: string): boolean {
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (
      (code >= 0x4E00 && code <= 0x9FFF) // CJK Unified Ideographs
      || (code >= 0x3400 && code <= 0x4DBF) // CJK Unified Ideographs Extension A
      || (code >= 0x20000 && code <= 0x2A6DF) // CJK Unified Ideographs Extension B
      || (code >= 0xF900 && code <= 0xFAFF) // CJK Compatibility Ideographs
      || (code >= 0x2F800 && code <= 0x2FA1F) // CJK Compatibility Ideographs Supplement
      || (code >= 0x3000 && code <= 0x303F) // CJK Symbols and Punctuation
      || (code >= 0x3040 && code <= 0x309F) // Hiragana
      || (code >= 0x30A0 && code <= 0x30FF) // Katakana
      || (code >= 0xAC00 && code <= 0xD7AF) // Hangul Syllables
    ) {
      return true;
    }
  }
  return false;
}

export function textAppearsTranslated(
  text: string | null,
  targetLanguage: string,
): boolean {
  if (!text || text.length <= 10) {
    return true;
  }
  if (targetLanguage.startsWith("zh")) {
    return hasCjk(text);
  }
  return true;
}

export function hasValidTranslation(
  payload: ArtifactPayload,
  targetLanguage: string,
): boolean {
  const hasTitle = payload.title !== null;
  const hasSummary = payload.publisherSummary !== null;
  if (!hasTitle && !hasSummary) {
    return false;
  }
  if (hasTitle && !textAppearsTranslated(payload.title, targetLanguage)) {
    return false;
  }
  if (hasSummary && !textAppearsTranslated(payload.publisherSummary, targetLanguage)) {
    return false;
  }
  return true;
}
