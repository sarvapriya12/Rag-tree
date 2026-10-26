"use client";

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let browserClient: SupabaseClient | null = null;

export function supabaseBrowser(): SupabaseClient | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) return null;
  if (!browserClient) browserClient = createClient(url, key, {
    auth: { flowType: "pkce", detectSessionInUrl: false, persistSession: true },
  });
  return browserClient;
}

export async function startSupabaseSignIn(provider: "github" | "google", link = false) {
  const client = supabaseBrowser();
  if (!client) throw new Error("Supabase sign-in is not configured");
  if (link) sessionStorage.setItem("devhive_supabase_link_started", String(Date.now()));
  else sessionStorage.removeItem("devhive_supabase_link_started");
  const redirectTo = `${window.location.origin}/auth/callback`;
  const { error } = await client.auth.signInWithOAuth({ provider, options: { redirectTo } });
  if (error) {
    sessionStorage.removeItem("devhive_supabase_link_started");
    throw error;
  }
}

export async function sendPasswordRecovery(email: string) {
  const client = supabaseBrowser();
  if (!client) throw new Error("Password recovery is not configured");
  const { error } = await client.auth.signInWithOtp({
    email: email.trim().toLowerCase(),
    options: { emailRedirectTo: `${window.location.origin}/auth/reset-password` },
  });
  if (error) throw error;
}
