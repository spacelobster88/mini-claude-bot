import { NextRequest, NextResponse } from "next/server";

/**
 * Push endpoint is no longer used — the local backend pushes directly to
 * Edge Config using the fresh CLI token. Kept as a no-op so old callers
 * get a clear message instead of a 404.
 */
export async function POST(req: NextRequest) {
  return NextResponse.json(
    { ok: false, error: "deprecated — backend pushes directly to Edge Config now" },
    { status: 410 },
  );
}

