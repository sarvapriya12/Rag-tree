import "server-only";

export const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export async function backendFetch(path: string, init: RequestInit = {}) {
  return fetch(`${BACKEND_URL}${path}`, { ...init, cache: "no-store" });
}

export function publicOrigin(request: Request): string {
  const host = request.headers.get("host") ?? new URL(request.url).host;
  const scheme = request.headers.get("x-forwarded-proto") ?? new URL(request.url).protocol.slice(0, -1);
  return `${scheme}://${host}`;
}

export function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    const from = new URL(origin);
    return from.origin === publicOrigin(request);
  } catch {
    return false;
  }
}
