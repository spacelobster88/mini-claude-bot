import { list } from "@vercel/blob";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const { blobs } = await list({ prefix: "metrics/current.json" });

    if (blobs.length === 0) {
      return NextResponse.json({ error: "no data yet" }, { status: 404 });
    }

    const blobUrl = blobs[0].url;
    const resp = await fetch(blobUrl, { cache: "no-store" });
    if (!resp.ok) {
      return NextResponse.json({ error: "blob fetch failed", status: resp.status }, { status: 502 });
    }

    const payload = await resp.json();
    return NextResponse.json(payload, {
      headers: { "Cache-Control": "no-cache, no-store, must-revalidate" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "metrics fetch failed", detail: message }, { status: 500 });
  }
}
