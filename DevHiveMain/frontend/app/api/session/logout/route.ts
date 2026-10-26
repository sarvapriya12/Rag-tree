import { NextRequest, NextResponse } from "next/server";
import { backendFetch, sameOrigin } from "@/lib/backend";
import { clearBrowserSession, getBrowserSession } from "@/lib/session";

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Origin denied" }, { status: 403 });
  const session = await getBrowserSession();
  if (session) await backendFetch("/auth/logout", { method: "POST",
    headers: { Authorization: `Bearer ${session.token}` } }).catch(() => undefined);
  await clearBrowserSession();
  return NextResponse.json({ ok: true });
}
