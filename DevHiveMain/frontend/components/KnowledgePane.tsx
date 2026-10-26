"use client";

import { ReactNode, useState } from "react";
import AnswerMarkdown from "@/components/AnswerMarkdown";
import CodeView from "@/components/CodeView";
import { DEPTH_LABEL } from "@/lib/depth";
import type { Answer, FileInsight, Outline, TeamMember } from "@/types";

type OpenFile = (path: string, range?: { start: number; end: number } | null) => void;

function shortDate(value?: string | null) {
  if (!value) return "unknown date";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value.slice(0, 10) :
    date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function splitPath(path: string) {
  const cut = path.lastIndexOf("/");
  return { dir: cut >= 0 ? path.slice(0, cut + 1) : "", name: path.slice(cut + 1) };
}

function Chip({ label }: { label?: string }) {
  return label ? <span className="source-chip">{label}</span> : null;
}

function InsightCard({ file, id, flashing, onOpen }: { file: FileInsight; id: string; flashing: boolean; onOpen?: OpenFile }) {
  const [expanded, setExpanded] = useState(false);
  const { dir, name } = splitPath(file.path);
  const range = file.start_line && file.end_line ? { start: file.start_line, end: file.end_line } : null;
  const focus = file.focus_start && file.focus_end ? { start: file.focus_start, end: file.focus_end } : range;
  const related = [...file.imports.map(path => ({ path, kind: "includes" })), ...file.used_by.map(path => ({ path, kind: "used by" }))];
  const lineCount = file.code.split("\n").length;
  return <article className={`insight ${flashing ? "flash" : ""}`} id={id}>
    <header className="insight-head">
      <div className="insight-title"><Chip label={file.source_label}/>
        <strong title={file.path}>{name}</strong>{file.symbol && <code className="insight-symbol">{file.symbol}</code>}</div>
      <div className="insight-meta"><span className="insight-dir" title={file.path}>{dir || "repository root"}</span>
        {range && <span className="insight-lines">lines {range.start}–{range.end}</span>}
        {file.score !== undefined && file.score > 0 && <span className="insight-score" title={`Relevance ${Math.round(file.score * 100)}%`}>
          <i style={{ width: `${Math.max(6, Math.round(file.score * 100))}%` }}/></span>}</div>
      {((file.sections ?? 1) > 1 || (file.duplicates?.length ?? 0) > 0) && <p className="insight-note">
        {(file.sections ?? 1) > 1 && <>Merged from {file.sections} neighbouring sections. </>}
        {(file.duplicates?.length ?? 0) > 0 && <>Same code also in {file.duplicates!.slice(0, 2).join(", ")}{file.duplicates!.length > 2 ? "…" : ""}.</>}</p>}
      <div className="insight-actions">
        {onOpen && <button type="button" className="mini-button" onClick={() => onOpen(file.path, range)}>Open in explorer</button>}
        {lineCount > 14 && <button type="button" className="mini-button" aria-expanded={expanded}
          onClick={() => setExpanded(value => !value)}>{expanded ? "Collapse code" : `Show all ${lineCount} lines`}</button>}
        <a className="mini-button" href={file.github_url} target="_blank" rel="noreferrer">GitHub ↗</a></div>
    </header>
    <div className={`insight-code ${expanded ? "insight-code--open" : ""}`}>
      <CodeView code={file.code} path={file.path} startLine={file.start_line} compact highlight={focus}/></div>
    {file.contributors.length > 0 && <div className="insight-people" aria-label="Contributors to this file">
      {file.contributors.slice(0, 5).map(person => <span className="person-chip" key={person.person}
        title={person.last_at ? `Last change ${shortDate(person.last_at)}` : undefined}>
        <i>{person.person.slice(0, 1).toUpperCase()}</i>{person.person}<b>{person.commits}</b></span>)}</div>}
    {file.history.length > 0 && <details className="insight-history">
      <summary>History · {file.history.length} change{file.history.length === 1 ? "" : "s"}
        <small>latest {shortDate(file.history[0].committed_at)} by {file.history[0].author}</small></summary>
      <ol className="timeline">{file.history.map(change => <li key={change.sha}>
        <span className="timeline-node"/>
        <div><p>{change.url ? <a href={change.url} target="_blank" rel="noreferrer">{change.message || "(no message)"}</a> : change.message || "(no message)"}</p>
          <small>{change.author} · {shortDate(change.committed_at)} · <code>{change.sha.slice(0, 7)}</code>
            {(change.additions !== undefined || change.deletions !== undefined) &&
              <> · <span className="plus">+{change.additions ?? 0}</span> <span className="minus">−{change.deletions ?? 0}</span></>}</small></div></li>)}</ol>
    </details>}
    {file.thoughts.length > 0 && <div className="insight-thoughts">{file.thoughts.map((thought, index) =>
      <p key={index}><strong>{thought.author}</strong> {thought.body}</p>)}</div>}
    {related.length > 0 && onOpen && <div className="insight-related"><span>Related</span>{related.slice(0, 6).map(item =>
      <button type="button" key={`${item.kind}:${item.path}`} onClick={() => onOpen(item.path, null)} title={item.kind}>{splitPath(item.path).name}</button>)}</div>}
  </article>;
}

function TeamCard({ team, label, id, flashing }: { team: TeamMember[]; label?: string; id: string; flashing: boolean }) {
  return <article className={`insight team-card ${flashing ? "flash" : ""}`} id={id}>
    <header className="insight-head"><div className="insight-title"><Chip label={label}/><strong>Contributors on this branch</strong></div></header>
    <ul className="team-list">{team.map(member => <li key={member.person}>
      <i className="avatar avatar-small">{member.person.slice(0, 1).toUpperCase()}</i>
      <div><strong>{member.person}</strong>
        <small>{member.commits} commit{member.commits === 1 ? "" : "s"} · {shortDate(member.first_at)}{member.last_at && member.last_at !== member.first_at ? ` → ${shortDate(member.last_at)}` : ""}</small>
        {member.files.length > 0 && <small className="team-files">{member.files.slice(0, 3).map(file => splitPath(file).name).join(", ")}</small>}</div></li>)}</ul>
  </article>;
}

function OutlineCard({ outline, id, flashing, onPickFolder }: { outline: Outline; id: string; flashing: boolean;
  onPickFolder?: (path: string) => void }) {
  return <article className={`insight outline-card ${flashing ? "flash" : ""}`} id={id}>
    <header className="insight-head"><div className="insight-title"><Chip label={outline.source_label}/><strong>Every folder in scope</strong></div>
      <p className="insight-note">Each folder is covered: with code excerpts, or by its summary line when it does not match the question.
        {onPickFolder ? " Click a folder to narrow the scope to it." : ""}</p></header>
    <ul className="outline-list">{outline.folders.map(folder => <li key={folder.path}>
      <div className="outline-row">
        {onPickFolder && folder.path !== "(root)" ? <button type="button" onClick={() => onPickFolder(folder.path)}>{folder.path}</button> : <span>{folder.path}</span>}
        {folder.excerpts !== undefined && <em className={`outline-tag ${folder.excerpts ? "outline-tag--code" : ""}`}>
          {folder.excerpts ? "code excerpts" : "summary only"}</em>}
        {folder.relevance !== undefined && folder.relevance > 0 && <span className="insight-score" title={`Match ${Math.round(folder.relevance * 100)}%`}>
          <i style={{ width: `${Math.max(6, Math.round(folder.relevance * 100))}%` }}/></span>}
      </div>
      <small>{folder.files} file{folder.files === 1 ? "" : "s"} · {folder.chunks} sections
        {folder.symbols?.length ? ` · ${folder.symbols.slice(0, 3).join(", ")}` : ""}</small>
      {folder.readme && <p>{folder.readme}</p>}</li>)}</ul>
  </article>;
}

export default function KnowledgePane({ answer, index, flash, onOpenFile, onPickFolder, onCite, side }: {
  answer: Answer | null; index: number; flash: string | null; onOpenFile?: OpenFile;
  onPickFolder?: (path: string) => void; onCite: (label: string) => void; side?: ReactNode;
}) {
  if (!answer) return <div className="knowledge__scroll">
    <p className="muted">The matching code with line numbers, who changed it and when, and related commits appear here after a question. Each card is merged from neighbouring sections so you see real context.</p>
    {side}</div>;
  const files = answer.files ?? [];
  const idFor = (label?: string) => (label ? `ev-${index}-${label}` : undefined);
  const commits = answer.citations.filter(item => item.kind !== "file");
  const groups = new Map<string, FileInsight[]>();
  for (const file of files) groups.set(file.group || "Code", [...(groups.get(file.group || "Code") ?? []), file]);
  const stats = answer.retrieval;
  return <div className="knowledge__scroll">
    {stats && <p className="knowledge__stats">
      <strong>{stats.folders_covered}</strong> of {stats.folders_in_scope} folder{stats.folders_in_scope === 1 ? "" : "s"} covered
      {stats.folders_with_code !== undefined && <> · <strong>{stats.folders_with_code}</strong> with code</>}
      {" "}· {stats.windows} section{stats.windows === 1 ? "" : "s"}
      {stats.avg_excerpt_chars ? ` · ~${Math.max(1, Math.round(stats.avg_excerpt_chars / 32))} lines per folder` : ""}
      {answer.depth ? ` · ${DEPTH_LABEL[answer.depth]}` : ""}
      {stats.llm_calls ? ` · ${stats.llm_calls} AI call${stats.llm_calls === 1 ? "" : "s"}` : ""}</p>}
    {answer.outline && <details className="kgroup kgroup--outline" open>
      <summary><strong>Folder coverage</strong><span>{answer.outline.folders.length} folder{answer.outline.folders.length === 1 ? "" : "s"}</span></summary>
      <OutlineCard outline={answer.outline} id={idFor(answer.outline.source_label) ?? "outline"}
        flashing={!!answer.outline.source_label && flash === answer.outline.source_label} onPickFolder={onPickFolder}/></details>}
    {answer.groups && answer.groups.length > 0 && <details className="kgroup kgroup--notes" open>
      <summary><strong>Folder summaries</strong><span>{answer.groups.length} part{answer.groups.length === 1 ? "" : "s"}</span></summary>
      {answer.groups.map(note => <section className="kgroup__note" key={note.key}>
        <h4>{note.folders.join(", ")}</h4><AnswerMarkdown text={note.summary} onCite={onCite}/></section>)}
    </details>}
    {[...groups.entries()].map(([name, rows]) => <details className="kgroup" open key={name}>
      <summary><strong title={name}>{name}</strong><span>{rows.length} location{rows.length === 1 ? "" : "s"}</span></summary>
      <div className="kgroup__cards">{rows.map(file => <InsightCard key={`${file.path}:${file.start_line}`} file={file}
        id={idFor(file.source_label) ?? file.path} flashing={!!file.source_label && flash === file.source_label} onOpen={onOpenFile}/>)}</div>
    </details>)}
    {files.length === 0 && answer.snippets?.map((snippet, i) => <article className={`insight ${flash && flash === snippet.source_label ? "flash" : ""}`}
      id={idFor(snippet.source_label)} key={`${snippet.path}:${i}`}>
      <header className="insight-head"><div className="insight-title"><Chip label={snippet.source_label}/>
        <span className="insight-path"><strong>{snippet.path}</strong></span></div>
        <div className="insight-meta">{snippet.start_line && <span>line {snippet.start_line}</span>}<span>{snippet.kind}</span></div></header>
      <CodeView code={snippet.code} path={snippet.path} startLine={snippet.start_line} compact/></article>)}
    {answer.team && answer.team.length > 0 && <TeamCard team={answer.team} label={answer.team_label} id={idFor(answer.team_label) ?? "team"}
      flashing={!!answer.team_label && flash === answer.team_label}/>}
    {commits.map(item => <article className={`insight source-card ${flash && flash === item.source_label ? "flash" : ""}`}
      id={idFor(item.source_label)} key={item.id}>
      <header className="insight-head"><div className="insight-title"><Chip label={item.source_label}/>
        <strong>{item.message.split("\n")[0].slice(0, 120)}</strong></div></header>
      <p className="source-meta">{item.kind === "tcc" ? "Recorded thought" : "Commit"} · {item.author ?? "unknown"} · {shortDate(item.committed_at)} · <code>{item.sha.slice(0, 7)}</code></p>
      <p className="source-excerpt">{item.excerpt.slice(0, 380)}</p></article>)}
    {answer.thoughts && answer.thoughts.filter(t => !files.some(f => f.thoughts.some(ft => ft.body === t.body))).map((thought, i) =>
      <article className="insight source-card" key={`thought:${i}`} id={idFor(thought.source_label)}>
        <header className="insight-head"><div className="insight-title"><Chip label={thought.source_label}/><strong>{thought.author}</strong></div></header>
        <p className="source-excerpt">{thought.body}</p></article>)}
    {side}
  </div>;
}
