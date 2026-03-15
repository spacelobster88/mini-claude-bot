import { list } from "@vercel/blob";
import { NextResponse } from "next/server";

export async function GET() {
  try {
    // Find the metrics blob
    const { blobs } = await list({ prefix: "metrics_current" });
    if (blobs.length === 0) {
      return NextResponse.json({ error: "no data yet" }, { status: 404 });
    }

    // Fetch the blob content
    const blob = blobs[0];
    const res = await fetch(blob.url);
    if (!res.ok) {
      return NextResponse.json({ error: "failed to read metrics" }, { status: 500 });
    }

    const metrics = await res.json();
    return NextResponse.json(metrics);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "blob read failed", detail: message }, { status: 500 });
  }
}
