"use client";

import { type CSSProperties, useMemo, useState } from "react";
import { FileIcon, FolderIcon } from "@/components/TreeIcons";
import { buildScopeTree, stateOf, summarize, toggle, type ScopeNode } from "@/lib/scope";
import type { ScopeFile } from "@/types";

type Row = { node: ScopeNode; depth: number; open: boolean };

function rowsFor(root: ScopeNode, expanded: Set<string>, filter: string): Row[] {
  const needle = filter.trim().toLowerCase();
  const memo = new Map<string, boolean>();
  // A node is shown while filtering when it, or anything below it, matches.
  const hasMatch = (node: ScopeNode): boolean => {
    let found = memo.get(node.path);
    if (found === undefined) {
      found = node.path.toLowerCase().includes(needle) || node.children.some(hasMatch);
      memo.set(node.path, found);
    }
    return found;
  };
  const rows: Row[] = [];
  const walk = (node: ScopeNode, depth: number) => {
    for (const child of node.children) {
      if (needle && !hasMatch(child)) continue;
      const open = !child.file && (expanded.has(child.path) || (!!needle && child.children.some(hasMatch)));
      rows.push({ node: child, depth, open });
      if (open) walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return rows;
}

export default function ScopeTree({ files, selected, onChange, hits, ready = true, onClose }: {
  files: ScopeFile[]; selected: string[]; onChange: (next: string[]) => void;
  /** Paths of files that appear in the current answer's evidence. */
  hits?: Set<string>; ready?: boolean; onClose?: () => void;
}) {
  const root = useMemo(() => buildScopeTree(files), [files]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const rows = useMemo(() => rowsFor(root, expanded, filter), [root, expanded, filter]);
  const hitCounts = useMemo(() => {
    const counts = new Map<string, number>();
    hits?.forEach(path => {
      const parts = path.split("/");
      for (let i = 1; i <= parts.length; i++) {
        const key = parts.slice(0, i).join("/");
        counts.set(key, (counts.get(key) ?? 0) + 1);
      }
    });
    return counts;
  }, [hits]);
  const chosen = useMemo(() => summarize(files, selected), [files, selected]);

  function flip(path: string) {
    setExpanded(current => {
      const next = new Set(current);
      if (!next.delete(path)) next.add(path);
      return next;
    });
  }

  return <div className="scope">
    <div className="scope__head">
      <div><p className="eyebrow">SCOPE</p><h3>Where to look</h3></div>
      {onClose && <button type="button" className="scope__close" onClick={onClose} aria-label="Close the scope panel">×</button>}
    </div>
    <div className="scope__tools">
      <input type="search" className="scope__search" value={filter} onChange={event => setFilter(event.target.value)}
        placeholder="Filter folders and files" aria-label="Filter folders and files" />
      <div className="scope__chips">
        <button type="button" className={`scope__all ${selected.length === 0 ? "scope__all--on" : ""}`}
          aria-pressed={selected.length === 0} onClick={() => onChange([])}>Whole repository</button>
        {selected.length > 0 && <button type="button" className="text-button" onClick={() => onChange([])}>Clear selection</button>}
      </div>
      <p className="scope__summary" aria-live="polite">
        {files.length === 0 ? "Nothing to select yet" :
          selected.length === 0 ? `Searching all ${chosen.files} files · ${chosen.chunks} sections` :
          `${selected.length} selected · ${chosen.files} file${chosen.files === 1 ? "" : "s"} · ${chosen.chunks} sections`}
      </p>
    </div>
    {files.length === 0 ? <p className="scope__empty">{ready ?
      "No indexed files on this branch." : "This branch is not indexed yet. Choose Sync memory in Repositories first."}</p> :
      rows.length === 0 ? <p className="scope__empty">No folder or file matches “{filter}”.</p> :
      <ul className="scope__tree" role="tree" aria-label="Folders and files to search" aria-multiselectable="true">
        {rows.map(({ node, depth, open }) => {
          const state = stateOf(node.path, selected);
          const hit = hitCounts.get(node.path) ?? 0;
          return <li key={node.path} role="treeitem" aria-level={depth + 1} aria-expanded={node.file ? undefined : open}
            aria-checked={state === "all" ? true : state === "some" ? "mixed" : false}
            className={`scope-row ${state === "all" ? "scope-row--selected" : state === "some" ? "scope-row--partial" : ""}`}
            style={{ "--depth": depth } as CSSProperties}>
            {node.file ? <span className="scope-row__chev scope-row__chev--blank" aria-hidden="true"/> :
              <button type="button" className={`scope-row__chev ${open ? "scope-row__chev--open" : ""}`}
                aria-label={`${open ? "Collapse" : "Expand"} ${node.name}`} onClick={() => flip(node.path)}>›</button>}
            <button type="button" className="scope-row__body" onClick={() => onChange(toggle(root, selected, node.path))}
              title={`${node.path} · ${node.file ? `${node.chunks} sections` : `${node.files} files, ${node.chunks} sections`}`}>
              <span className={`scope-check scope-check--${state}`} aria-hidden="true"/>
              {node.file ? <FileIcon name={node.name}/> : <FolderIcon open={open}/>}
              <span className="scope-row__name">{node.name}</span>
              {hit > 0 && <span className="scope-row__hit" title={`${hit} retrieved in the last answer`}>{node.file ? "" : hit}</span>}
              <span className="scope-row__count">{node.file ? node.chunks : node.files}</span>
            </button>
          </li>;
        })}
      </ul>}
  </div>;
}
