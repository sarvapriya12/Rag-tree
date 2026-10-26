import { NextRequest, NextResponse } from "next/server";
import { backendFetch, sameOrigin } from "@/lib/backend";
import { getBrowserSession } from "@/lib/session";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context) {
  if (request.method !== "GET" && !sameOrigin(request)) {
    return NextResponse.json({ detail: "Origin denied" }, { status: 403 });
  }
  const session = await getBrowserSession();
  if (!session) return NextResponse.json({ detail: "Sign in required" }, { status: 401 });
  const { path } = await context.params;
  if (path.some((part) => part === ".." || part === ".")) {
    return NextResponse.json({ detail: "Invalid path" }, { status: 400 });
  }
  const body = request.method === "GET" ? undefined : await request.text();
  if (body && body.length > 50_000) return NextResponse.json({ detail: "Request too large" }, { status: 413 });
  try {
    const upstream = await backendFetch(`/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, {
      method: request.method, body,
      headers: { Authorization: `Bearer ${session.token}`,
        ...(body ? { "content-type": request.headers.get("content-type") ?? "application/json" } : {}) },
    });
    const text = await upstream.text();
    return new NextResponse(text || null, { status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json",
        "cache-control": "no-store" } });
  } catch {
    return NextResponse.json({ detail: "Backend unavailable" }, { status: 502 });
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
