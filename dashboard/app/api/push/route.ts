import { NextRequest, NextResponse } from "next/server";

const EDGE_CONFIG_ID = process.env.EDGE_CONFIG_ID;
const VERCEL_API_TOKEN = process.env.VERCEL_API_TOKEN;

async function upsertEdgeConfig(items: any[]) {
  if (!EDGE_CONFIG_ID || !VERCEL_API_TOKEN) {
    throw new Error("missing edge config env");
  }

  const res = await fetch(`https://api.vercel.com/v1/edge-config/${EDGE_CONFIG_ID}/items`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${VERCEL_API_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ items }),
  });

  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`edge config write failed: ${res.status} ${detail}`);
  }
}

export async function POST(req: NextRequest) {
  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${process.env.METRICS_SECRET}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const metrics = await req.json();
  const timestamp = new Date().toISOString();

  try {
    await upsertEdgeConfig([
      { operation: "upsert", key: "metrics_current", value: metrics },
      { operation: "upsert", key: "metrics_last_push", value: timestamp },
    ]);
    return NextResponse.json({ ok: true, pushed_at: timestamp });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "edge config write failed", detail: message }, { status: 500 });
  }
}
