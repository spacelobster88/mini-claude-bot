import { get } from "@vercel/edge-config";
import { NextResponse } from "next/server";

export async function GET() {
  const raw = await get<string>("metrics_current");
  const lastPush = await get<string>("metrics_last_push");

  if (!raw) {
    return NextResponse.json({ error: "no data yet" }, { status: 404 });
  }

  const metrics = typeof raw === "string" ? JSON.parse(raw) : raw;
  return NextResponse.json({ ...metrics, _last_push: lastPush });
}
