import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const GIST_ID = "293db39a0a328d56069caf8bdb279c51";

export async function GET() {
  try {
    // Use GitHub API (not raw URL) to avoid CDN caching
    const resp = await fetch(`https://api.github.com/gists/${GIST_ID}`, {
      cache: "no-store",
      headers: {
        Accept: "application/vnd.github+json",
        "User-Agent": "mini-claude-bot-dashboard",
      },
    });

    if (!resp.ok) {
      return NextResponse.json({ error: "gist fetch failed", status: resp.status }, { status: 502 });
    }

    const gist = await resp.json();
    const file = gist.files?.["metrics.json"];
    if (!file?.content) {
      return NextResponse.json({ error: "no metrics data in gist" }, { status: 404 });
    }

    const payload = JSON.parse(file.content);
    return NextResponse.json(payload, {
      headers: { "Cache-Control": "no-cache, no-store, must-revalidate" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "metrics fetch failed", detail: message }, { status: 500 });
  }
}
