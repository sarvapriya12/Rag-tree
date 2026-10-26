"use client";

import { useEffect, useRef, useState } from "react";
import { supabaseBrowser } from "@/lib/supabase-browser";

export default function SupabaseCallback() {
  const started = useRef(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    async function finish() {
      const client = supabaseBrowser();
      if (!client) throw new Error("Supabase sign-in is not configured");
      const current = new URL(window.location.href);
      const fragment = new URLSearchParams(window.location.hash.slice(1));
      const authError = current.searchParams.get("error_description") || current.searchParams.get("error") ||
        fragment.get("error_description") || fragment.get("error");
      const linkStarted = Number(sessionStorage.getItem("devhive_supabase_link_started"));
      sessionStorage.removeItem("devhive_supabase_link_started");
      const link = fragment.get("type") !== "invite" && linkStarted > 0 &&
        Date.now() - linkStarted < 10 * 60 * 1000;
      window.history.replaceState(null, "", "/auth/callback");
      if (authError) throw new Error(authError);
      const code = current.searchParams.get("code");
      if (!code && !fragment.has("access_token"))
        throw new Error("No sign-in response was returned. Check the Google and Supabase redirect URLs.");
      if (code) {
        const { error: exchangeError } = await client.auth.exchangeCodeForSession(code);
        if (exchangeError) throw exchangeError;
      } else if (fragment.has("access_token")) {
        const access_token = fragment.get("access_token");
        const refresh_token = fragment.get("refresh_token");
        if (!access_token || !refresh_token) throw new Error("Invitation link is incomplete");
        const { error: inviteError } = await client.auth.setSession({ access_token, refresh_token });
        if (inviteError) throw inviteError;
      }
      const { data, error: sessionError } = await client.auth.getSession();
      if (sessionError || !data.session?.access_token) throw new Error("No Supabase session was returned");
      const response = await fetch(link ? "/api/session/supabase/link" : "/api/session/supabase", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ access_token: data.session.access_token }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Supabase sign-in failed");
      window.location.replace("/");
    }
    finish().catch(problem => setError(problem instanceof Error ? problem.message : "Sign-in failed"));
  }, []);

  return <main className="auth-page"><section className="auth-panel" style={{ margin: "auto" }}>
    <div className="auth-card"><h1>{error ? "Could not sign in" : "Finishing sign-in…"}</h1>
      {error && <><p role="alert">{error}</p><a href="/login">Return to sign in</a></>}
    </div></section></main>;
}
