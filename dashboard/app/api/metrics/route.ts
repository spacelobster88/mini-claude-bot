import { get } from "@vercel/edge-config";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const raw = await get<string>("metrics_current");
    const lastPush = await get<string>("metrics_last_push");

    if (!raw) {
      return NextResponse.json({ error: "no data yet" }, { status: 404 });
    }

    const payload = typeof raw === "string" ? JSON.parse(raw) : raw;
    return NextResponse.json(
      { ...payload, _last_push: lastPush ?? payload?._last_push ?? null },
      { headers: { "Cache-Control": "no-cache, no-store, must-revalidate" } },
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: "metrics fetch failed", detail: message }, { status: 500 });
  }
}
