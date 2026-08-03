import type { Metadata } from "next";
import snapshot from "@/data/latest.json";
import { Reader } from "./reader";

export const metadata: Metadata = {
  title: "Aaron Reader — AI labs, without the noise",
  description:
    "A deterministic feed for OpenAI, Anthropic, and Claude updates, with automatically refreshed multilingual AI enrichment.",
  alternates: {
    canonical: "/",
    types: {
      "application/rss+xml": "/reader/feed.xml",
    },
  },
  openGraph: {
    title: "Aaron Reader",
    description:
      "The work behind the AI frontier, collected at 09:15 and 21:15 San Francisco time.",
    url: "/",
    images: [{ url: "/og.png", width: 1200, height: 630 }],
    type: "website",
  },
};

export default function Home() {
  return <Reader snapshot={snapshot} />;
}
