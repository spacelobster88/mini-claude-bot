import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

const GIST_ID = "293db39a0a328d56069caf8bdb279c51";
const RAW_URL = `https://gist.githubusercontent.com/spacelobster88/${GIST_ID}/raw/metrics.json`;

export async function GET() {
  try {
    const resp = await fetch(RAW_URL, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });

    if (!resp.ok) {
      return NextResponse.json({ error: "gist fetch failed", status: resp.status }, { status: 502 });
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
