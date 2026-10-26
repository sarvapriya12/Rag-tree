"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import CodeView from "@/components/CodeView";
import { FileIcon, FolderIcon } from "@/components/TreeIcons";

type FileResult = { path: string; branch: string; content: string; github_url?: string; indexed_snapshot?: boolean };
type Note = { id: string; body: string; author: string; can_edit: boolean; updated_at: string };
type Thought = { id: string; body: string; author: string; kind: string; created_at: string };
type Change = { sha: string; author: string; message: string; committed_at: string | null };
type Review = { notes: Note[]; thoughts: Thought[]; changes: Change[]; imports: string[]; used_by: string[] };
type Answer = { summary: string; snippets: { path: string; code: string; start_line: number | null; kind: string }[];
  thoughts: Thought[]; changes: Change[]; imports: string[]; used_by: string[]; indexed: boolean };
type TreeNode = { name: string; path: string; children: Map<string, TreeNode>; file: boolean; size: number };

function makeTree(files: { path: string; size: number }[]) {
  const root: TreeNode = { name: "", path: "", children: new Map(), file: false, size: 0 };
  for (const file of files) {
    let current = root;
    for (const [index, part] of file.path.split("/").entries()) {
      const path = current.path ? `${current.path}/${part}` : part;
      if (!current.children.has(part)) current.children.set(part, {
        name: part, path, children: new Map(), file: index === file.path.split("/").length - 1, size: file.size,
      });
      current = current.children.get(part)!;
    }
  }
  return root;
}

function TreeRow({ node, selected, onSelect, depth = 0 }: { node: TreeNode; selected: string | null;
  onSelect: (path: string) => void; depth?: number }) {
  const [open, setOpen] = useState(true);
  const children = [...node.children.values()].sort((a, b) => Number(a.file) - Number(b.file) || a.name.localeCompare(b.name));
  return <>
    {node.path && <button type="button" className={`tree-row ${selected === node.path ? "active" : ""}`}
      style={{ paddingLeft: 12 + depth * 16 }} onClick={() => node.file ? onSelect(node.path) : setOpen(!open)}
      title={node.file ? `${node.path} · ${node.size} bytes` : node.path}>
      {!node.file && <span className={`tree-chevron ${open ? "open" : ""}`}>›</span>}
      {node.file ? <FileIcon name={node.name}/> : <FolderIcon open={open}/>}<span>{node.name}</span>
    </button>}
    {(node.path === "" || open) && children.map(child => <TreeRow key={child.path} node={child}
      selected={selected} onSelect={onSelect} depth={node.path ? depth + 1 : depth}/>) }
  </>;
}

export default function FileReview({ repoId, branch, files, partial, file, onSelect, focus }: {
  repoId: string; branch: string; files: { path: string; size: number }[]; partial: boolean;
  file: FileResult | null; onSelect: (path: string) => void;
  /** Lines to emphasise and scroll to when the file was opened from an answer. */
  focus?: { path: string; start: number; end: number } | null;
}) {
  const tree = useMemo(() => makeTree(files), [files]);
  const [review, setReview] = useState<Review | null>(null);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [question, setQuestion] = useState("");
  const [thought, setThought] = useState("");
  const [pathQuery, setPathQuery] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setReview(null); setAnswer(null); setEditing(null); setError("");
    if (!file || file.branch !== branch) return;
    api<Review>(`/repositories/${repoId}/file-review?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(file.path)}`)
      .then(setReview).catch(problem => setError(problem.message));
  }, [repoId, branch, file?.path, file?.branch]);

  async function reloadReview() {
    if (!file) return;
    setReview(await api<Review>(`/repositories/${repoId}/file-review?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(file.path)}`));
  }

  async function saveThought(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true); setError("");
    try {
      if (editing) await api(`/repositories/${repoId}/file-review/notes/${editing}`, "PUT", { body: thought });
      else await api(`/repositories/${repoId}/file-review/notes`, "POST", { branch, path: file.path, body: thought });
      setThought(""); setEditing(null);
      await reloadReview();
      setAnswer(null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not save thought"); }
    finally { setBusy(false); }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!file) return;
    setBusy(true); setError("");
    try {
      setAnswer(await api<Answer>(`/repositories/${repoId}/file-chat`, "POST", { branch, path: file.path, message: question }));
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not answer for this file"); }
    finally { setBusy(false); }
  }

  const thoughts = answer?.thoughts ?? review?.thoughts ?? [];
  return <section className="file-review-shell" id="file-review">
    <div className="file-review-heading"><div><p className="eyebrow">SOURCE EXPLORER</p><h3>Browse and review files</h3>
      <p className="muted">Read code, trace imports, and discuss changes. Repository files are read only in DevHive.</p></div>
      <span className="count-badge">{files.length}{partial ? "+" : ""} files</span></div>
    <div className="explorer-grid">
      <aside className="explorer-tree" aria-label="Repository file tree">
        <div className="explorer-tree-title"><FolderIcon open/> <strong>{branch}</strong></div>
        <form className="explorer-open-path" onSubmit={event => { event.preventDefault(); if (pathQuery.trim()) onSelect(pathQuery.trim()); }}>
          <input aria-label="Open file by path" value={pathQuery} onChange={event => setPathQuery(event.target.value)}
            placeholder="Open path…"/><button type="submit" title="Open path">↵</button>
        </form>
        {files.length ? <div className="explorer-tree-scroll"><TreeRow node={tree} selected={file?.path ?? null} onSelect={onSelect}/></div> :
          <p className="muted explorer-empty">No files loaded for this branch.</p>}
        {partial && <p className="hint explorer-empty">GitHub returned a partial file tree.</p>}
      </aside>
      <div className="explorer-code">{file ? <CodeView code={file.content} path={file.path} githubUrl={file.github_url}
        highlight={focus && focus.path === file.path ? { start: focus.start, end: focus.end } : null}/> :
        <div className="code-placeholder"><FileIcon name="source.ts"/><h3>Select a file</h3><p>Choose a file in the tree to read its code and review context.</p></div>}</div>
    </div>
    {file && <div className="review-workspace">
      {file.indexed_snapshot && <p className="hint" role="status">GitHub could not be reached, so this is the snapshot from the last sync. Line numbers match the answers you received.</p>}
      <div className="review-header"><div><p className="eyebrow">LIVE CODE REVIEW</p><h3>{file.path}</h3></div>
        {file.github_url && <a className="text-link" href={file.github_url} target="_blank" rel="noreferrer">View on GitHub ↗</a>}</div>
      <div className="review-context"><div><strong>Imports</strong>{review?.imports.length ? review.imports.map(path =>
        <button className="review-file-link" key={path} onClick={() => onSelect(path)}>{path}</button>) : <span>None indexed</span>}</div>
        <div><strong>Used by</strong>{review?.used_by.length ? review.used_by.map(path =>
          <button className="review-file-link" key={path} onClick={() => onSelect(path)}>{path}</button>) : <span>None indexed</span>}</div></div>
      <form className="review-question" onSubmit={ask}><label>Ask about this file<input value={question}
        onChange={event => setQuestion(event.target.value)} placeholder="What changed here, and why?" minLength={2} maxLength={300} required /></label>
        <button className="button button-primary" disabled={busy}>Ask file memory</button></form>
      {error && <p className="error" role="alert">{error}</p>}
      {answer && <div className="file-answer" role="region" aria-label="File memory answer">
        <div className="file-answer-top"><div className="answer-brief"><p className="eyebrow">CODE BRIEF</p><p>{answer.summary}</p>
          <p className="hint">Answer focused on {file.path}. Connected code is shown only when indexed and relevant.</p></div>
          <div className="answer-code"><p className="eyebrow">RELEVANT CODE</p>{answer.snippets.map((snippet, index) =>
            <CodeView key={`${snippet.path}:${index}`} code={snippet.code} path={snippet.path} startLine={snippet.start_line}/>)}</div></div>
        <div className="answer-thoughts"><p className="eyebrow">THOUGHTS & CONTRIBUTORS</p>
          {thoughts.length ? thoughts.map(item => <div className="thought-row" key={`${item.kind}:${item.id}`}>
            <span className="avatar avatar-small">{item.author.slice(0, 1).toUpperCase()}</span><div><strong>{item.author}</strong>
              <small>{item.kind} · {new Date(item.created_at).toLocaleDateString()}</small><p>{item.body}</p></div></div>) :
            <p className="muted">No thoughts recorded for this file yet.</p>}
          {answer.changes.length > 0 && <p className="hint">Recent indexed change: {answer.changes[0].message} by {answer.changes[0].author}.</p>}
        </div>
      </div>}
      <div className="review-notes"><div><p className="eyebrow">TEAM THOUGHTS</p><h3>{review?.notes.length ?? 0} notes on this file</h3></div>
        {review?.notes.map(note => <div className="thought-row" key={note.id}><span className="avatar avatar-small">{note.author.slice(0, 1).toUpperCase()}</span>
          <div><strong>{note.author}</strong><small>Updated {new Date(note.updated_at).toLocaleDateString()}</small><p>{note.body}</p></div>
          {note.can_edit && <button className="text-button" onClick={() => { setEditing(note.id); setThought(note.body); }}>Edit thought</button>}</div>)}
        <form className="stack" onSubmit={saveThought}><label>{editing ? "Edit your thought" : "Add a thought"}
          <textarea value={thought} onChange={event => setThought(event.target.value)} minLength={2} maxLength={5000}
            placeholder="What does this file do? What should the next reviewer know?" required /></label>
          <div className="review-note-actions"><button className="button button-secondary" disabled={busy}>{editing ? "Save changes" : "Post thought"}</button>
            {editing && <button type="button" className="text-button" onClick={() => { setEditing(null); setThought(""); }}>Cancel</button>}</div></form>
      </div>
    </div>}
  </section>;
}
