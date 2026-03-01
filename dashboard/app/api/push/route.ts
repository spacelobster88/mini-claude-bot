import { NextRequest, NextResponse } from "next/server";

export async function POST(req: NextRequest) {
  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${process.env.METRICS_SECRET}`) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const metrics = await req.json();
  const ecfgId = process.env.EDGE_CONFIG_ID;
  const apiToken = process.env.VERCEL_API_TOKEN;

  if (!ecfgId || !apiToken) {
    return NextResponse.json({ error: "missing edge config env vars" }, { status: 500 });
  }

  const res = await fetch(`https://api.vercel.com/v1/edge-config/${ecfgId}/items`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${apiToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      items: [
        { operation: "upsert", key: "metrics_current", value: JSON.stringify(metrics) },
        { operation: "upsert", key: "metrics_last_push", value: new Date().toISOString() },
      ],
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    return NextResponse.json({ error: "edge config write failed", detail: err }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}
