import { NextRequest, NextResponse } from "next/server";
import { backendFetch, sameOrigin } from "@/lib/backend";
import { createBrowserSession } from "@/lib/session";

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Origin denied" }, { status: 403 });
  const payload = await request.text();
  if (payload.length > 10_000) return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  try {
    const response = await backendFetch("/auth/login", { method: "POST", body: payload,
      headers: { "content-type": "application/json" } });
    const data = await response.json();
    if (!response.ok) return NextResponse.json(data, { status: response.status });
    await createBrowserSession(data.user.id, data.token, data.expires_at);
    return NextResponse.json({ user: data.user });
  } catch {
    return NextResponse.json({ detail: "Backend unavailable" }, { status: 502 });
  }
}
