import { get, list } from "@vercel/blob";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const BLOB_TOKEN = process.env.BLOB_READ_WRITE_TOKEN ?? process.env.VERCEL_BLOB_READ_WRITE_TOKEN ?? process.env.BLOB_TOKEN;

async function fetchMetricsFromBlob(blobPath: string) {
  const res = await get(blobPath, { access: "private", token: BLOB_TOKEN, useCache: false });
  if (!res?.stream) throw new Error("blob stream unavailable");
  const reader = res.stream.getReader();
  const decoder = new TextDecoder();
  let result = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    result += decoder.decode(value, { stream: true });
  }
  if (!result) throw new Error("empty metrics blob");
  return JSON.parse(result);
}

export async function GET() {
  try {
    const { blobs } = await list({ prefix: "metrics_current", token: BLOB_TOKEN });
    if (blobs.length === 0) {
      return NextResponse.json({ error: "no data yet" }, { status: 404 });
    }

    const latest = [...blobs]
      .sort((a, b) => new Date(b.uploadedAt ?? "").getTime() - new Date(a.uploadedAt ?? "").getTime())[0] ?? blobs[0];
    const blobPath = latest.pathname ?? latest.url;
    if (!blobPath) throw new Error("metrics blob missing pathname");
    const metrics = await fetchMetricsFromBlob(blobPath);
    return NextResponse.json(metrics, {
      headers: { "Cache-Control": "no-cache, no-store, must-revalidate" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "blob read failed", detail: message }, { status: 500 });
  }
}
