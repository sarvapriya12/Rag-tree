"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { supabaseBrowser } from "@/lib/supabase-browser";

export default function ResetPasswordPage() {
  const started = useRef(false);
  const [accessToken, setAccessToken] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    async function readLink() {
      const client = supabaseBrowser();
      if (!client) throw new Error("Password recovery is not configured");
      const current = new URL(window.location.href);
      const fragment = new URLSearchParams(window.location.hash.slice(1));
      const authError = current.searchParams.get("error_description") || current.searchParams.get("error") ||
        fragment.get("error_description") || fragment.get("error");
      const code = current.searchParams.get("code");
      const access = fragment.get("access_token");
      const refresh = fragment.get("refresh_token");
      window.history.replaceState(null, "", "/auth/reset-password");
      if (authError) throw new Error(authError);
      if (!code && !(access && refresh))
        throw new Error("This recovery link is missing or expired. Request a new link.");
      if (code) {
        const { error: exchangeError } = await client.auth.exchangeCodeForSession(code);
        if (exchangeError) throw exchangeError;
      } else {
        const { error: sessionError } = await client.auth.setSession({ access_token: access!, refresh_token: refresh! });
        if (sessionError) throw sessionError;
      }
      const { data, error: sessionError } = await client.auth.getSession();
      if (sessionError || !data.session?.access_token)
        throw new Error("Could not verify this recovery link. Request a new link.");
      setAccessToken(data.session.access_token);
    }
    readLink().catch(problem => setError(problem instanceof Error ? problem.message : "Recovery failed"));
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (password !== confirm) { setError("Passwords do not match"); return; }
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/session/password/reset", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ access_token: accessToken, new_password: password }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not reset password");
      setComplete(true);
      setPassword("");
      setConfirm("");
      setAccessToken("");
      await supabaseBrowser()?.auth.signOut().catch(() => {});
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Could not reset password");
    } finally {
      setBusy(false);
    }
  }

  return <main className="auth-page"><section className="auth-panel" style={{ margin: "auto" }}>
    <div className="auth-card">
      <div className="brand" style={{ color: "var(--ink)", marginBottom: 32 }}><span className="brand-mark">D</span> DevHive</div>
      <h1>{complete ? "Password updated" : "Choose a new password"}</h1>
      {complete ? <p className="muted" role="status">Your password has been reset. Sign in with the new password.</p> : <>
        <p className="muted">Use at least 12 characters. Your other DevHive sessions will be signed out.</p>
        {accessToken && <form className="stack" onSubmit={submit}>
          <label>New password<input type="password" autoComplete="new-password" minLength={12} maxLength={200}
            value={password} onChange={event => setPassword(event.target.value)} required /></label>
          <label>Confirm password<input type="password" autoComplete="new-password" minLength={12} maxLength={200}
            value={confirm} onChange={event => setConfirm(event.target.value)} required /></label>
          <button className="button button-primary" disabled={busy}>{busy ? "Updating…" : "Reset password"}</button>
        </form>}
        {!accessToken && !error && <p className="muted">Checking recovery link…</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </>}
      <p className="auth-switch"><Link className="text-link" href="/login">Return to sign in</Link></p>
    </div>
  </section></main>;
}
