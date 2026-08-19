import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://aaron-reader.aaron-he-zhu.workers.dev/"),
  title: "Aaron Reader — AI labs, without the noise",
  description: "Official OpenAI and Anthropic posts, in English and 简体中文. We only translate the title and short blurb — click through to read the original.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
  openGraph: {
    title: "Aaron Reader — AI labs, without the noise",
    description: "Official OpenAI and Anthropic posts, in English and 简体中文. We only translate the title and short blurb — click through to read the original.",
    type: "website",
  },
};

export const viewport: Viewport = {
  colorScheme: "light",
  themeColor: "#f2f0ea",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
