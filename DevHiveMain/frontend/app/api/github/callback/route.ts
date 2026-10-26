import { timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { backendFetch, publicOrigin } from "@/lib/backend";
import { getBrowserSession } from "@/lib/session";

function equal(a: string, b: string) {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

export async function GET(request: NextRequest) {
  const destination = new URL("/", publicOrigin(request));
  const response = (reason: string) => {
    destination.searchParams.set("github_error", reason);
    const redirect = NextResponse.redirect(destination);
    for (const name of ["devhive_github_state", "devhive_github_verifier", "devhive_github_user"])
      redirect.cookies.set(name, "", { path: "/api/github", maxAge: 0 });
    return redirect;
  };
  const state = request.nextUrl.searchParams.get("state") ?? "";
  const code = request.nextUrl.searchParams.get("code") ?? "";
  const saved = request.cookies.get("devhive_github_state")?.value ?? "";
  const verifier = request.cookies.get("devhive_github_verifier")?.value ?? "";
  const userId = request.cookies.get("devhive_github_user")?.value ?? "";
  const session = await getBrowserSession();
  if (!state || !saved || !equal(state, saved) || !code || !verifier ||
      !session || session.backendUserId !== userId) return response("authorization_failed");
  const redirectUrl = process.env.GITHUB_OAUTH_REDIRECT_URL;
  if (!redirectUrl || new URL(redirectUrl).origin !== publicOrigin(request)) return response("configuration");
  try {
    const exchange = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST", cache: "no-store", headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: process.env.GITHUB_CLIENT_ID,
        client_secret: process.env.GITHUB_CLIENT_SECRET, code, redirect_uri: redirectUrl, code_verifier: verifier }),
    });
    const payload = await exchange.json();
    if (!exchange.ok || !payload.access_token || payload.error ||
        !(payload.scope ?? "").split(",").map((scope: string) => scope.trim()).includes("repo"))
      return response("authorization_failed");
    const stored = await backendFetch("/auth/github", { method: "PUT", headers: {
      Authorization: `Bearer ${session.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ token: payload.access_token }) });
    if (!stored.ok) return response("connection_failed");
    destination.searchParams.set("github", "connected");
    const redirect = NextResponse.redirect(destination);
    for (const name of ["devhive_github_state", "devhive_github_verifier", "devhive_github_user"])
      redirect.cookies.set(name, "", { path: "/api/github", maxAge: 0 });
    return redirect;
  } catch {
    return response("connection_failed");
  }
}
