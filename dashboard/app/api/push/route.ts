import { NextRequest, NextResponse } from "next/server";

const GIST_ID = "293db39a0a328d56069caf8bdb279c51";

export async function POST(req: NextRequest) {
  const secret = process.env.METRICS_SECRET;
  if (!secret) {
    return NextResponse.json({ ok: false, error: "server misconfigured" }, { status: 500 });
  }

  const auth = req.headers.get("authorization");
  if (auth !== `Bearer ${secret}`) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }

  const ghToken = process.env.GITHUB_TOKEN;
  if (!ghToken) {
    return NextResponse.json({ ok: false, error: "missing GITHUB_TOKEN" }, { status: 500 });
  }

  try {
    const metrics = await req.json();
    const timestamp = metrics.timestamp ?? new Date().toISOString();
    metrics._last_push = timestamp;

    const resp = await fetch(`https://api.github.com/gists/${GIST_ID}`, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${ghToken}`,
        "Content-Type": "application/json",
        Accept: "application/vnd.github+json",
      },
      body: JSON.stringify({
        files: {
          "metrics.json": { content: JSON.stringify(metrics) },
        },
      }),
    });

    if (!resp.ok) {
      const body = await resp.text();
      return NextResponse.json({ ok: false, error: `github: ${resp.status}`, detail: body }, { status: 502 });
    }

    return NextResponse.json({ ok: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error: message }, { status: 500 });
  }
}
