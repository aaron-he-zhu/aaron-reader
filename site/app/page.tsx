import type { Metadata } from "next";
import snapshot from "@/data/latest.json";
import { Reader } from "./reader";

export const metadata: Metadata = {
  title: "Aaron Reader — AI labs, without the noise",
  description:
    "Official OpenAI and Anthropic posts, in English and 简体中文. We only translate the title and short blurb — click through to read the original.",
  alternates: {
    canonical: "/",
    types: {
      "application/rss+xml": "/reader/feed.xml",
    },
  },
  openGraph: {
    title: "Aaron Reader — AI labs, without the noise",
    description:
      "Official OpenAI and Anthropic posts, in English and 简体中文. We only translate the title and short blurb — click through to read the original.",
    url: "/",
    images: [{ url: "/og.png", width: 1200, height: 630 }],
    type: "website",
  },
};

export default function Home() {
  return <Reader snapshot={snapshot} />;
}
