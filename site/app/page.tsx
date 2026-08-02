import type { Metadata } from "next";
import snapshot from "@/data/latest.json";
import { Reader } from "./reader";

export const metadata: Metadata = {
  title: "Aaron Reader — AI labs, without the noise",
  description:
    "A deterministic, token-free feed for OpenAI, Anthropic, and Claude updates, with optional GPT-5.6 Luna enrichment.",
  alternates: {
    types: {
      "application/rss+xml": "/reader/feed.xml",
    },
  },
  openGraph: {
    title: "Aaron Reader",
    description:
      "The work behind the AI frontier, collected at 10:00 and 22:00 San Francisco time.",
    images: [{ url: "/og.png", width: 1200, height: 630 }],
    type: "website",
  },
};

export default function Home() {
  return <Reader snapshot={snapshot} />;
}
