import { redirect } from "next/navigation";
import { backendFetch } from "@/lib/backend";
import { getBrowserSession } from "@/lib/session";
import Workspace from "@/components/Workspace";
import type { User } from "@/types";

export default async function Home() {
  const session = await getBrowserSession();
  if (!session) redirect("/login");
  let user: User;
  try {
    const response = await backendFetch("/auth/me", {
      headers: { Authorization: `Bearer ${session.token}` },
    });
    if (!response.ok) redirect("/login");
    user = await response.json();
  } catch (error) {
    if (error && typeof error === "object" && "digest" in error) throw error;
    return <main className="unavailable"><h1>DevHive is waiting for the API</h1>
      <p>Start the FastAPI backend, then refresh this page.</p></main>;
  }
  return <Workspace user={user} />;
}
