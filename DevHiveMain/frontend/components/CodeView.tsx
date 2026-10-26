"use client";

import { useEffect, useRef, useState } from "react";
import { Highlight, Prism, themes } from "prism-react-renderer";

const LANGUAGES: Record<string, string> = {
  c: "c", h: "cpp", cc: "cpp", cpp: "cpp", cxx: "cpp", hh: "cpp", hpp: "cpp", hxx: "cpp",
  m: "objectivec", mm: "objectivec", java: "cpp", cs: "cpp", kt: "kotlin", swift: "swift",
  py: "python", ts: "tsx", tsx: "tsx", js: "jsx", jsx: "jsx", json: "json", yml: "yaml", yaml: "yaml",
  md: "markdown", css: "css", html: "markup", xml: "markup", go: "go", rs: "rust", sql: "sql",
};

function languageFor(path: string): string {
  const language = LANGUAGES[path.split(".").pop()?.toLowerCase() ?? ""] ?? "plain";
  return language === "plain" || Prism.languages[language] ? language : "plain";
}

const LINE_HEIGHT = 22;

export default function CodeView({ code, path, startLine = 1, highlight, githubUrl, compact = false }: {
  code: string; path: string; startLine?: number | null;
  /** Inclusive line numbers (as shown in the gutter) to emphasise and scroll to. */
  highlight?: { start: number; end: number } | null;
  githubUrl?: string | null; compact?: boolean;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const first = startLine ?? 1;

  useEffect(() => {
    if (!highlight || !scroller.current) return;
    const offset = (highlight.start - first) * LINE_HEIGHT;
    scroller.current.scrollTop = Math.max(0, offset - scroller.current.clientHeight / 3);
  }, [highlight?.start, highlight?.end, first, code]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch { /* clipboard can be unavailable; the code stays selectable */ }
  }

  return <div className={`code-view ${compact ? "compact" : ""}`} aria-label={`Read-only source code for ${path}`}>
    <div className="code-toolbar"><span className="code-file-dot"/><strong title={path}>{compact ? path.split("/").pop() : path}</strong>
      <span className="code-tools">
        {githubUrl && <a href={githubUrl} target="_blank" rel="noreferrer" title="Open on GitHub">GitHub ↗</a>}
        <button type="button" onClick={copy} aria-label="Copy code">{copied ? "Copied" : "Copy"}</button>
      </span></div>
    <div className="code-scroll" ref={scroller}>
      <Highlight theme={themes.vsDark} code={code.replace(/\n$/, "")} language={languageFor(path)}>
        {({ tokens, getTokenProps }) => <div className="code-lines">{tokens.map((line, index) => {
          const number = startLine ? first + index : null;
          const marked = !!highlight && number !== null && number >= highlight.start && number <= highlight.end;
          return <div key={index} className={`code-line ${marked ? "marked" : ""}`}>
            <span className="code-line-number">{number ?? "·"}</span>
            <code>{line.length === 1 && line[0].empty ? " " : line.map((token, i) => {
              const props = getTokenProps({ token });
              return <span key={i} className={props.className} style={props.style}>{props.children}</span>;
            })}</code></div>;
        })}</div>}
      </Highlight>
    </div>
  </div>;
}
