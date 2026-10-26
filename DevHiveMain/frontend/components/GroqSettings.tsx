"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { LlmSettings } from "@/types";

export default function GroqSettings() {
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api<LlmSettings>("/auth/llm").then(setSettings).catch(() => setError("Could not load AI settings."));
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setMessage("");
    try {
      setSettings(await api<LlmSettings>("/auth/llm", "PUT", { api_key: key.trim() }));
      setKey("");
      setMessage("Your Groq key is saved for your account.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Could not save key.");
    } finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError(""); setMessage("");
    try {
      await api("/auth/llm", "DELETE");
      setSettings(current => current && { ...current, configured: false, active: !!current.shared });
      setKey("");
      setMessage("Your Groq key was removed.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Could not remove key.");
    } finally { setBusy(false); }
  }

  return <div className="panel groq-settings"><p className="eyebrow">AI EXPLANATIONS</p>
    <h2>Groq explanations</h2>
    <p className="muted">{settings?.shared ? "This workspace already has an AI key, so everyone gets explanations without setup. You can add your own key to use your own Groq quota instead. " : "Add your own Groq API key to get explanations of the code and memory you can access. "}
      Relevant snippets and your question are sent to Groq when you ask; likely secrets are never sent.</p>
    <p className="connection-state">{settings?.configured ? "Your own key is in use" : settings?.shared ? "Workspace key active" : "No key configured"} · {settings?.model ?? "openai/gpt-oss-120b"}</p>
    <form className="stack" onSubmit={save}><label>Groq API key<input type="password" value={key}
      onChange={event => setKey(event.target.value)} minLength={20} maxLength={300}
      autoComplete="new-password" spellCheck={false} placeholder="gsk_..." required /></label>
      <div className="groq-actions"><button className="button button-primary" disabled={busy || !key.trim()}>{settings?.shared ? "Use my own key" : "Save key"}</button>
        {settings?.configured && <button type="button" className="button button-secondary" disabled={busy} onClick={remove}>Remove my key</button>}</div>
    </form>
    <p className="hint">Get your key at <a href="https://console.groq.com/keys" target="_blank" rel="noopener noreferrer">Groq Console</a>. DevHive never shows a saved key again.</p>
    {message && <p className="hint" role="status">{message}</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
