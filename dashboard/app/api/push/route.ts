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

  const metrics = trimMetrics(await req.json());
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

function trimMetrics(payload: any) {
  if (!payload || typeof payload !== 'object') return payload;
  const data = JSON.parse(JSON.stringify(payload));

  if (Array.isArray(data.cron_jobs)) {
    const allowedKeys = new Set(['id', 'name', 'cron_expression', 'enabled', 'last_run_at', 'last_result_preview', 'timezone']);
    data.cron_jobs = data.cron_jobs.slice(0, 8).map((job: any) => {
      const trimmed: Record<string, any> = {};
      for (const key of allowedKeys) {
        if (job?.[key] !== undefined) trimmed[key] = job[key];
      }
      if (typeof trimmed.last_result_preview === 'string' && trimmed.last_result_preview.length > 160) {
        trimmed.last_result_preview = `${trimmed.last_result_preview.slice(0, 100)}…`;
      }
      return trimmed;
    });
  }

  if (data.memory?.items && Array.isArray(data.memory.items)) {
    const perCategory = 1;
    const totalLimit = 10;
    const counts: Record<string, number> = {};
    const limited: any[] = [];
    for (const item of data.memory.items) {
      if (limited.length >= totalLimit) break;
      const cat = item?.category ?? 'general';
      counts[cat] = counts[cat] ?? 0;
      if (counts[cat] >= perCategory) continue;
      counts[cat]++;
      const trimmedItem = { ...item } as any;
      if (typeof trimmedItem.content === 'string' && trimmedItem.content.length > 160) {
        trimmedItem.content = `${trimmedItem.content.slice(0, 160)}…`;
      }
      limited.push(trimmedItem);
    }
    data.memory.items = limited;
  }

  if (data.claude_usage?.daily_activity && Array.isArray(data.claude_usage.daily_activity)) {
    data.claude_usage.daily_activity = data.claude_usage.daily_activity.slice(-14);
  }
  if (data.claude_usage?.model_usage && typeof data.claude_usage.model_usage === 'object') {
    const entries = Object.entries(data.claude_usage.model_usage)
      .sort((a, b) => (b[1]?.requests || 0) - (a[1]?.requests || 0))
      .slice(0, 2);
    data.claude_usage.model_usage = Object.fromEntries(entries);
  }

  if (data.harness) {
    for (const key of ['running_jobs', 'completed_jobs']) {
      if (Array.isArray(data.harness[key])) {
        data.harness[key] = data.harness[key].slice(0, 3);
      }
    }
  }

  return data;
}

