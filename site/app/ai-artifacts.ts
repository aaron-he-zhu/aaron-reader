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

export function artifactSearchText(artifacts: AIArtifact[] | undefined) {
  return (artifacts || []).flatMap((artifact) => {
    const payload = artifactPayload(artifact);
    return [payload.title, payload.publisherSummary, payload.summary, ...payload.keyPoints]
      .filter((value): value is string => value !== null);
  });
}
