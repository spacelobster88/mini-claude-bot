import { list } from "@vercel/blob";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const { blobs } = await list({ prefix: "metrics_current" });
    if (blobs.length === 0) {
      return NextResponse.json({ error: "no data yet" }, { status: 404 });
    }

    const blob = blobs[0];
    // Cache-bust: append timestamp to avoid CDN caching stale blob URLs
    const fetchUrl = `${blob.url}?t=${Date.now()}`;
    const res = await fetch(fetchUrl, { cache: "no-store" });
    if (!res.ok) {
      return NextResponse.json(
        { error: "failed to read metrics", status: res.status, url: blob.url },
        { status: 500 }
      );
    }

    const metrics = await res.json();
    return NextResponse.json(metrics, {
      headers: { "Cache-Control": "no-cache, no-store, must-revalidate" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "blob read failed", detail: message }, { status: 500 });
  }
}
