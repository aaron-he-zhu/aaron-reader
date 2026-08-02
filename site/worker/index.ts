/** Cloudflare Worker entry point for Aaron Reader. */
import handler from "vinext/server/app-router-entry";

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/reader/feed.xml") {
      const asset = await env.ASSETS.fetch(request);
      if (asset.ok && request.method !== "HEAD") {
        const feed = await asset.text();
        const publicHome = new URL("/", request.url).href;
        const body = feed.replace(
          "<link>http://127.0.0.1:8765/</link>",
          `<link>${escapeXml(publicHome)}</link>`,
        );
        const headers = new Headers(asset.headers);
        headers.set("content-type", "application/rss+xml; charset=utf-8");
        headers.set("cache-control", "public, max-age=300, must-revalidate");
        return withSecurityHeaders(new Response(body, { status: asset.status, headers }));
      }
      return withSecurityHeaders(asset);
    }

    return withSecurityHeaders(await handler.fetch(request, env, ctx));
  },
} satisfies ExportedHandler<Env>;

function escapeXml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function withSecurityHeaders(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set(
    "content-security-policy",
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  );
  headers.set("cross-origin-resource-policy", "same-origin");
  headers.set("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()");
  headers.set("referrer-policy", "no-referrer");
  headers.set("x-content-type-options", "nosniff");
  if (!headers.has("cache-control")) {
    headers.set("cache-control", "public, max-age=300, must-revalidate");
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export default worker;
