import { NextRequest, NextResponse } from "next/server";
import { backendFetch, sameOrigin } from "@/lib/backend";

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Origin denied" }, { status: 403 });
  const payload = await request.text();
  if (payload.length > 12_000) return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  try {
    const response = await backendFetch("/auth/password/reset", { method: "POST", body: payload,
      headers: { "content-type": "application/json" } });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Password recovery is unavailable" }, { status: 502 });
  }
}
