"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Markdown where [S1] becomes a chip that jumps to its evidence card. */
export default function AnswerMarkdown({ text, onCite }: { text: string; onCite: (label: string) => void }) {
  const source = text.replace(/\[(S\d+)\]/g, "[$1](#cite-$1)");
  return <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children }) => href?.startsWith("#cite-")
      ? <button type="button" className="cite-chip" onClick={() => onCite(href.slice(6))}>{children}</button>
      : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  }}>{source}</ReactMarkdown></div>;
}
