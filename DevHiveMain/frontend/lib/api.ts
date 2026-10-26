export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/backend${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail :
      Array.isArray(detail) ? detail.map(item => item.msg ?? "Invalid input").join(", ") :
      `Request failed (${response.status})`;
    throw new Error(message);
  }
  return payload as T;
}
