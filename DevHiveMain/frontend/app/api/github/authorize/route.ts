import { createHash, randomBytes } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { publicOrigin, sameOrigin } from "@/lib/backend";
import { getBrowserSession } from "@/lib/session";

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return NextResponse.json({ error: "Invalid origin" }, { status: 403 });
  const session = await getBrowserSession();
  if (!session) return NextResponse.json({ error: "Sign in first" }, { status: 401 });
  const clientId = process.env.GITHUB_CLIENT_ID;
  const redirect = process.env.GITHUB_OAUTH_REDIRECT_URL;
  if (!clientId || !redirect || !process.env.GITHUB_CLIENT_SECRET) {
    return NextResponse.json({ error: "GitHub authorization is not configured" }, { status: 503 });
  }
  const callback = new URL(redirect);
  if (callback.origin !== publicOrigin(request) || callback.pathname !== "/api/github/callback") {
    return NextResponse.json({ error: "GitHub callback URL does not match this site" }, { status: 503 });
  }
  const state = randomBytes(32).toString("base64url");
  const verifier = randomBytes(32).toString("base64url");
  const challenge = createHash("sha256").update(verifier).digest("base64url");
  const url = new URL("https://github.com/login/oauth/authorize");
  url.search = new URLSearchParams({ client_id: clientId, redirect_uri: redirect,
    scope: "repo", state, code_challenge: challenge, code_challenge_method: "S256" }).toString();
  const response = NextResponse.json({ url: url.toString() });
  const cookie = { httpOnly: true, sameSite: "lax" as const, secure: process.env.NODE_ENV === "production",
    path: "/api/github", maxAge: 600 };
  response.cookies.set("devhive_github_state", state, cookie);
  response.cookies.set("devhive_github_verifier", verifier, cookie);
  response.cookies.set("devhive_github_user", session.backendUserId, cookie);
  return response;
}
