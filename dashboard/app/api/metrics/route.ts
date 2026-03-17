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

    const latest = [...blobs]
      .sort((a, b) => new Date(b.uploadedAt ?? '').getTime() - new Date(a.uploadedAt ?? '').getTime())[0]
      ?? blobs[0];
    const blob = latest;
    const signedUrl = blob.downloadUrl;
    const token = process.env.BLOB_READ_WRITE_TOKEN || process.env.VERCEL_BLOB_READ_WRITE_TOKEN || process.env.BLOB_TOKEN;

    let fetchUrl = blob.url;
    const headers: Record<string, string> = {};

    if (signedUrl) {
      fetchUrl = signedUrl;
    } else {
      fetchUrl = `${blob.url}?t=${Date.now()}`;
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }
    }

    const res = await fetch(fetchUrl, {
      cache: 'no-store',
      headers: Object.keys(headers).length ? headers : undefined,
    });
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
