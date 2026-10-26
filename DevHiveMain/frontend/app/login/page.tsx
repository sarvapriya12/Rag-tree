"use client";

import { FormEvent, useState } from "react";
import { startSupabaseSignIn } from "@/lib/supabase-browser";
import Link from "next/link";

export default function LoginPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const supabaseEnabled = Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL &&
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY);

  async function oauth(provider: "github" | "google") {
    setError(""); setBusy(true);
    try { await startSupabaseSignIn(provider); }
    catch (problem) {
      setError(problem instanceof Error ? problem.message : "Supabase sign-in failed");
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const response = await fetch(`/api/session/${mode}`, { method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(mode === "register" ? { name, email, password } : { email, password }) });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Unable to sign in");
      window.location.assign("/");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Something went wrong");
      setBusy(false);
    }
  }

  return <main className="auth-page"><section className="auth-intro">
    <div className="brand brand-light"><span className="brand-mark">D</span> DevHive</div>
    <p className="eyebrow">ENGINEERING MEMORY</p>
    <h1>Keep the reason behind every change.</h1>
    <p>Bring repositories, teammates, permissions and decisions together in one quiet workspace.</p>
    <div className="auth-steps"><span>01&nbsp; Projects</span><span>02&nbsp; Repositories</span><span>03&nbsp; Memory</span></div>
  </section><section className="auth-panel"><div className="auth-card">
    <p className="eyebrow">WELCOME TO DEVHIVE</p>
    <h2>{mode === "login" ? "Sign in" : "Create an account"}</h2>
    <p className="muted">{mode === "login" ? "Return to your engineering workspace." : "Start a new workspace for your team."}</p>
    <form onSubmit={submit} className="stack">
      {mode === "register" && <label>Your name<input value={name} onChange={e => setName(e.target.value)} required minLength={2} /></label>}
      <label>Email address<input type="email" value={email} onChange={e => setEmail(e.target.value)} required autoComplete="email" /></label>
      <label>Password<input type="password" value={password} onChange={e => setPassword(e.target.value)}
        required minLength={mode === "register" ? 12 : 1} autoComplete={mode === "login" ? "current-password" : "new-password"} /></label>
      {mode === "login" && <Link className="text-link" href="/auth/forgot-password">Forgot password?</Link>}
      {mode === "register" && <p className="hint">Use at least 12 characters.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      <button className="button button-primary" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
    </form>
    {supabaseEnabled && <div className="stack"><p className="muted">Or sign in with Supabase</p>
      <button type="button" className="button button-secondary" disabled={busy} onClick={() => oauth("github")}>Continue with GitHub</button>
      <button type="button" className="button button-secondary" disabled={busy} onClick={() => oauth("google")}>Continue with Google</button>
    </div>}
    <p className="auth-switch">{mode === "login" ? "New to DevHive?" : "Already have an account?"} <button className="text-button" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
      {mode === "login" ? "Create an account" : "Sign in"}</button></p>
  </div></section></main>;
}
