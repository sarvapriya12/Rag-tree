import type { Depth } from "@/types";

/**
 * Depth sets quality, never reach: every level covers every folder in the selected scope (the whole
 * repository when nothing is selected). Mirrors DEPTH_PROFILES in backend/app/retrieval.py.
 */
export const DEPTHS: { key: Depth; label: string; caption: string; detail: string }[] = [
  { key: "low", label: "Low", caption: "Brief",
    detail: "Every folder in scope, one short excerpt each, and a brief answer. One AI call, a few seconds." },
  { key: "medium", label: "Medium", caption: "Balanced",
    detail: "Every folder in scope with up to two sections each and their surrounding code, and a short paragraph per folder." },
  { key: "high", label: "High", caption: "Detailed",
    detail: "Every folder in scope with up to three larger sections each, file history included, and a detailed section per folder. May wait up to a minute for AI capacity on the free tier." },
  { key: "very_high", label: "Very high", caption: "Deepest",
    detail: "Every folder in scope read most deeply: up to five sections each, summarised folder by folder, then combined. 4 to 5 AI calls: about 1 to 2 minutes on the free Groq tier." },
];

export const DEPTH_LABEL: Record<Depth, string> = Object.fromEntries(DEPTHS.map(d => [d.key, d.label])) as Record<Depth, string>;

export function isDepth(value: unknown): value is Depth {
  return DEPTHS.some(d => d.key === value);
}
