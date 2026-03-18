import { NextRequest, NextResponse } from "next/server";

export async function POST(req: NextRequest) {
  const secret = process.env.METRICS_SECRET;
  if (!secret) {
    return NextResponse.json({ ok: false, error: "server misconfigured" }, { status: 500 });
  }

  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${secret}`) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const vercelToken = process.env.VERCEL_API_TOKEN;
  const edgeConfigId = process.env.EDGE_CONFIG_ID;
  if (!vercelToken || !edgeConfigId) {
    return NextResponse.json({ ok: false, error: "missing edge config credentials" }, { status: 500 });
  }

  try {
    const metrics = await req.json();
    const timestamp = metrics.timestamp ?? new Date().toISOString();

    const resp = await fetch(
      `https://api.vercel.com/v1/edge-config/${edgeConfigId}/items`,
      {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${vercelToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          items: [
            { operation: "upsert", key: "metrics_current", value: metrics },
            { operation: "upsert", key: "metrics_last_push", value: timestamp },
          ],
        }),
      },
    );

    if (!resp.ok) {
      const body = await resp.text();
      return NextResponse.json({ ok: false, error: `edge config: ${resp.status}`, detail: body }, { status: 502 });
    }

    return NextResponse.json({ ok: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error: message }, { status: 500 });
  }
}
