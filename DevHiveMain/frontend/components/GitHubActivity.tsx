"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Contributor, GitHubActivityEvent, GitHubCommit } from "@/types";

const kinds = ["", "push", "branch_created", "branch_deleted", "pull_request"];

export default function GitHubActivity({ repoId, branches, branch }: {
  repoId: string; branches: string[]; branch: string;
}) {
  const [selectedBranch, setSelectedBranch] = useState("");
  const [actor, setActor] = useState("");
  const [kind, setKind] = useState("");
  const [applied, setApplied] = useState({ actor: "", kind: "" });
  const [events, setEvents] = useState<GitHubActivityEvent[]>([]);
  const [commits, setCommits] = useState<GitHubCommit[]>([]);
  const [contributors, setContributors] = useState<Contributor[]>([]);
  const [examined, setExamined] = useState(0);
  const [detail, setDetail] = useState<{ sha: string; files: { name: string; status: string;
    additions: number; deletions: number; patch: string }[] } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [eventsLoading, setEventsLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setEventsLoading(true); setEvents([]); setError("");
    api<GitHubActivityEvent[]>(`/repositories/${repoId}/activity`)
      .then(rows => { if (active) setEvents(rows); })
      .catch((problem: Error) => { if (active) setError(problem.message); })
      .finally(() => { if (active) setEventsLoading(false); });
    return () => { active = false; };
  }, [repoId]);

  useEffect(() => {
    let active = true;
    setLoading(true); setDetail(null); setCommits([]); setContributors([]);
    const historyBranch = selectedBranch || branch;
    (historyBranch ? Promise.all([
      api<GitHubCommit[]>(`/repositories/${repoId}/history?branch=${encodeURIComponent(historyBranch)}`),
      api<{ examined: number; contributors: Contributor[] }>(`/repositories/${repoId}/contributors?branch=${encodeURIComponent(historyBranch)}`),
    ]) : Promise.resolve([[], { examined: 0, contributors: [] }] as [GitHubCommit[], { examined: number; contributors: Contributor[] }]))
      .then(([rows, summary]) => { if (active) { setCommits(rows); setContributors(summary.contributors); setExamined(summary.examined); } })
      .catch((problem: Error) => { if (active) setError(problem.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [repoId, selectedBranch, branch]);

  function filter(event: FormEvent) { event.preventDefault(); setApplied({ actor: actor.trim(), kind }); }
  async function inspect(sha: string) {
    const targetBranch = selectedBranch || branch;
    if (!targetBranch) return;
    try {
      setDetail(await api(`/repositories/${repoId}/history/${sha}?branch=${encodeURIComponent(targetBranch)}`));
      setError("");
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not open commit"); }
  }
  const matchingCommits = commits.filter(row => !applied.actor || row.author.toLowerCase().includes(applied.actor.toLowerCase()) ||
    row.committer.toLowerCase().includes(applied.actor.toLowerCase()));
  const matchingEvents = events.filter(row => (!selectedBranch || row.branch === selectedBranch) &&
    (!applied.actor || row.actor.toLowerCase().includes(applied.actor.toLowerCase())) &&
    (!applied.kind || row.kind === applied.kind));

  return <div className="activity-layout">
    <div className="panel"><p className="eyebrow">CONTRIBUTORS</p><h2>Who changed what on {selectedBranch || branch}</h2>
      <p className="muted">Summary of the latest {examined} GitHub commits on this branch.</p>
      {contributors.filter(person => !applied.actor || person.person.toLowerCase().includes(applied.actor.toLowerCase())).map(person =>
        <div className="activity-item" key={person.person}><span className="avatar avatar-small">{person.person.slice(0, 1).toUpperCase()}</span>
          <div><strong>{person.person} · {person.commits} commit{person.commits === 1 ? "" : "s"}</strong>
            <small>{person.last_at ? `Last commit ${new Date(person.last_at).toLocaleString()}` : ""}</small>
            {person.recent.map(item => <p key={item.sha}>{item.message} <code>{item.sha.slice(0, 9)}</code>
              {item.files.length > 0 && <small> · {item.files.join(", ")}</small>}</p>)}</div>
        </div>)}
      {!loading && !contributors.length && <p className="muted">No contributions found in the recent GitHub history.</p>}
    </div>
    <div className="panel"><p className="eyebrow">GITHUB ACTIVITY</p><h2>Branch activity</h2>
      <p className="muted">Recent pushes, branch creation and deletion, and pull requests visible on your branches.</p>
      <form className="activity-filters" onSubmit={filter}>
        <label>Branch<select value={selectedBranch} onChange={e => setSelectedBranch(e.target.value)}>
          <option value="">All visible branches</option>{branches.map(name => <option key={name} value={name}>{name}</option>)}
        </select></label>
        <label>Person<input value={actor} onChange={e => setActor(e.target.value)} placeholder="GitHub login" /></label>
        <label>Activity<select value={kind} onChange={e => setKind(e.target.value)}>
          {kinds.map(value => <option key={value} value={value}>{value ? value.replaceAll("_", " ") : "All activity"}</option>)}
        </select></label>
        <button className="button button-primary">Apply filters</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {eventsLoading ? <p className="muted">Loading GitHub activity…</p> : matchingEvents.length ?
        <div className="activity-list">{matchingEvents.map(event => <div className="activity-item" key={event.id}>
          <span className="activity-icon">{event.kind === "push" ? "↑" : event.kind === "branch_created" ? "+" : event.kind === "branch_deleted" ? "−" : "↗"}</span>
          <div><strong>{event.actor} · {event.kind.replaceAll("_", " ")}</strong><p>{event.detail}</p>
            <small>{event.branch} · {event.created_at ? new Date(event.created_at).toLocaleString() : ""}</small></div>
          {event.sha && <code>{event.sha.slice(0, 9)}</code>}
        </div>)}</div> : <p className="muted">No recent activity matched these filters.</p>}
    </div>
    <div className="panel"><p className="eyebrow">LIVE GITHUB HISTORY</p><h2>Commits on {selectedBranch || branch}</h2>
      <p className="muted">Author, committer, and commit time come directly from GitHub. Inspect a commit to see changed files.</p>
      {matchingCommits.map(commit => <div className="activity-item" key={commit.sha}>
        <span className="timeline-dot"/><div><strong>{commit.message.split("\n")[0]}</strong>
          <small>{commit.author} · committed by {commit.committer} · {commit.committed_at ? new Date(commit.committed_at).toLocaleString() : ""}</small></div>
        <button className="text-button" onClick={() => inspect(commit.sha)}>Inspect {commit.sha.slice(0, 9)}</button>
      </div>)}
      {detail && <div className="commit-detail"><h3>Changed files · {detail.sha.slice(0, 9)}</h3>
        {detail.files.map(file => <details key={file.name}><summary>{file.status} · {file.name} <small>+{file.additions} −{file.deletions}</small></summary>
          <pre>{file.patch || "Patch unavailable for this file."}</pre></details>)}
      </div>}
      {!loading && !matchingCommits.length && <p className="muted">No commits matched this person on this branch.</p>}
    </div>
  </div>;
}
