"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { sendPasswordRecovery } from "@/lib/supabase-browser";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await sendPasswordRecovery(email);
      setSent(true);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Could not send the recovery email");
    } finally {
      setBusy(false);
    }
  }

  return <main className="auth-page"><section className="auth-panel" style={{ margin: "auto" }}>
    <div className="auth-card">
      <div className="brand" style={{ color: "var(--ink)", marginBottom: 32 }}><span className="brand-mark">D</span> DevHive</div>
      <h1>Reset your password</h1>
      {sent ? <p className="muted" role="status">Check your email for a recovery link. Open it in this browser, then choose a new DevHive password.</p> : <>
        <p className="muted">Enter the email address for your DevHive account.</p>
        <form className="stack" onSubmit={submit}>
          <label>Email address<input type="email" autoComplete="email" value={email}
            onChange={event => setEmail(event.target.value)} required /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="button button-primary" disabled={busy}>{busy ? "Sending…" : "Email recovery link"}</button>
        </form>
      </>}
      <p className="auth-switch"><Link className="text-link" href="/login">Return to sign in</Link></p>
    </div>
  </section></main>;
}
