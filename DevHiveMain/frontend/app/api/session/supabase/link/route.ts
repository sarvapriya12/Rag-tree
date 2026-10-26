import { NextRequest, NextResponse } from "next/server";
import { backendFetch, sameOrigin } from "@/lib/backend";
import { getBrowserSession } from "@/lib/session";

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Origin denied" }, { status: 403 });
  const session = await getBrowserSession();
  if (!session) return NextResponse.json({ detail: "Sign in to DevHive first" }, { status: 401 });
  const payload = await request.text();
  if (payload.length > 12_000) return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  try {
    const response = await backendFetch("/auth/supabase/link", { method: "POST", body: payload,
      headers: { "content-type": "application/json", authorization: `Bearer ${session.token}` } });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Supabase linking is unavailable" }, { status: 502 });
  }
}
