"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { startSupabaseSignIn, supabaseBrowser } from "@/lib/supabase-browser";
import GitHubActivity from "@/components/GitHubActivity";
import MemoryWorkspace from "@/components/MemoryWorkspace";
import FileReview from "@/components/FileReview";
import GroqSettings from "@/components/GroqSettings";
import type { Audit, Commit, GitHubRepository, Invitation, Member, Override, Project,
  PullRequest, Repository, Role, ScopeTreeData, User } from "@/types";

type Tab = "repositories" | "memory" | "activity" | "settings";
type FileResult = { path: string; branch: string; content: string; sha?: string; github_url?: string; indexed_snapshot?: boolean };
type SyncSchedule = { enabled: boolean; day: string; local_time: string; time_zone: string;
  next_run: string | null; last_indexed_at: string | null;
  embedding_mode: "semantic" | "lexical"; needs_reindex: boolean };
const BRANCH_PERMISSIONS = new Set(["push", "merge", "write_tcc", "create_branch"]);

function shortSha(sha: string) { return sha.slice(0, 9); }
function readableDate(value?: string | null) { return value ? new Date(value).toLocaleDateString() : "—"; }

export default function Workspace({ user: initialUser }: { user: User }) {
  const [user] = useState(initialUser);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState<string | null>(null);
  const [repo, setRepo] = useState<Repository | null>(null);
  const [workspaceTarget, setWorkspaceTarget] = useState<string | null>(null);
  const [roles, setRoles] = useState<Role[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [overrides, setOverrides] = useState<Override[]>([]);
  const [githubRepos, setGithubRepos] = useState<GitHubRepository[]>([]);
  const [tab, setTab] = useState<Tab>("repositories");
  const [settingsSection, setSettingsSection] = useState<"connections" | "people" | "permissions">("connections");
  const [sidebarWidth, setSidebarWidth] = useState(246);
  const dragStart = useRef<{ x: number; width: number } | null>(null);
  const [branch, setBranch] = useState("");
  const [branches, setBranches] = useState<string[]>([]);
  const [access, setAccess] = useState<Record<string, boolean>>({});
  const [commits, setCommits] = useState<Commit[]>([]);
  const [pulls, setPulls] = useState<PullRequest[]>([]);
  const [syncSchedule, setSyncSchedule] = useState<SyncSchedule | null>(null);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [githubStatus, setGithubStatus] = useState<{ connected: boolean; error: string | null } | null>(null);

  const [projectName, setProjectName] = useState("");
  const [chosenGithubRepo, setChosenGithubRepo] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [newRepoName, setNewRepoName] = useState("");
  const [newRepoPrivate, setNewRepoPrivate] = useState(true);
  const [file, setFile] = useState<FileResult | null>(null);
  const [fileFocus, setFileFocus] = useState<{ path: string; start: number; end: number } | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncVersion, setSyncVersion] = useState(0);
  const [treeFiles, setTreeFiles] = useState<{ path: string; size: number }[]>([]);
  const [treePartial, setTreePartial] = useState(false);
  // Indexed files with section counts: what a question can be scoped to.
  const [scopeTree, setScopeTree] = useState<ScopeTreeData | null>(null);
  const [newBranchName, setNewBranchName] = useState("");
  const [pullTitle, setPullTitle] = useState("");
  const [pullBase, setPullBase] = useState("");
  const [tccSha, setTccSha] = useState("");
  const [thought, setThought] = useState("");
  const [changeSummary, setChangeSummary] = useState("");
  const [comment, setComment] = useState("");
  const [memberEmail, setMemberEmail] = useState("");
  const [memberRoleId, setMemberRoleId] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRoleId, setInviteRoleId] = useState("");
  const [permissionCatalog, setPermissionCatalog] = useState<string[]>([]);
  const [editingRoleId, setEditingRoleId] = useState<string | null>(null);
  const [roleName, setRoleName] = useState("");
  const [roleColor, setRoleColor] = useState("#64748b");
  const [rolePermissions, setRolePermissions] = useState<string[]>([]);
  const [overrideRoleId, setOverrideRoleId] = useState("");
  const [overridePattern, setOverridePattern] = useState("main");
  const [overrideAllow, setOverrideAllow] = useState<string[]>([]);
  const [overrideDeny, setOverrideDeny] = useState<string[]>([]);

  const loadProjects = useCallback(async (preferred?: string) => {
    const rows = await api<Project[]>("/projects");
    setProjects(rows);
    setProjectId(current => preferred ?? (current && rows.some(p => p.id === current) ? current : rows[0]?.id ?? null));
  }, []);

  const loadProject = useCallback(async (id: string) => {
    const [p, repos, roleRows, memberRows, overrideRows] = await Promise.all([
      api<Project>(`/projects/${id}`), api<Repository[]>(`/projects/${id}/repositories`),
      api<Role[]>(`/projects/${id}/roles`), api<Member[]>(`/projects/${id}/members`),
      api<Override[]>(`/projects/${id}/branch-overrides`),
    ]);
    const inviteRows = p.is_owner ? await api<Invitation[]>(`/projects/${id}/invitations`) : [];
    setProject(p); setRepositories(repos); setRoles(roleRows); setMembers(memberRows);
    setInvitations(inviteRows); setOverrides(overrideRows);
    setRepoId(current => current && repos.some(r => r.id === current) ? current : repos[0]?.id ?? null);
  }, []);

  const loadRepo = useCallback(async (id: string) => {
    const r = await api<Repository>(`/repositories/${id}`);
    setRepo(r);
    setPullBase(r.default_branch);
    const names = await api<{ name: string }[]>(`/repositories/${id}/branches`).catch(() => []);
    setBranches(names.map(item => item.name));
    // Prefer the repository's default branch over whichever GitHub lists first.
    setBranch(current => current && names.some(item => item.name === current) ? current :
      names.find(item => item.name === r.default_branch)?.name ?? names[0]?.name ?? "");
  }, []);

  const loadPulls = useCallback(async (id: string) => {
    setPulls(await api<PullRequest[]>(`/repositories/${id}/pulls`));
  }, []);

  useEffect(() => {
    loadProjects().catch(problem => setError(problem.message));
    api<{ name: string }[]>("/permissions").then(rows => setPermissionCatalog(rows.map(r => r.name))).catch(() => undefined);
    const saved = Number(localStorage.getItem("devhive_sidebar_width"));
    if (saved >= 200 && saved <= 420) setSidebarWidth(saved);
  }, [loadProjects]);

  useEffect(() => {
    const url = new URL(window.location.href);
    const connected = url.searchParams.get("github") === "connected";
    const problem = url.searchParams.get("github_error");
    if (connected || problem) {
      setGithubStatus({ connected, error: problem });
      setTab("settings"); setSettingsSection("connections");
      url.searchParams.delete("github"); url.searchParams.delete("github_error");
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
  }, []);

  useEffect(() => {
    function move(event: PointerEvent) {
      if (!dragStart.current) return;
      setSidebarWidth(Math.min(420, Math.max(200, dragStart.current.width + event.clientX - dragStart.current.x)));
    }
    function stop() {
      if (dragStart.current) {
        const width = document.querySelector<HTMLElement>(".sidebar")?.offsetWidth;
        if (width) localStorage.setItem("devhive_sidebar_width", String(width));
      }
      dragStart.current = null;
    }
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, []);

  useEffect(() => {
    if (!projectId) { setProject(null); setRepositories([]); setRepoId(null); return; }
    setProject(null); setRepositories([]); setRepo(null); setRepoId(null);
    setBranches([]); setBranch("");
    loadProject(projectId).catch(problem => setError(problem.message));
  }, [projectId, loadProject]);

  useEffect(() => {
    if (!repoId) { setRepo(null); setCommits([]); return; }
    setRepo(null); setBranches([]); setBranch(""); setCommits([]); setAccess({});
    setFile(null); setTreeFiles([]); setScopeTree(null); setPulls([]); setSyncSchedule(null);
    loadRepo(repoId).catch(problem => setError(problem.message));
    loadPulls(repoId).catch(() => setPulls([]));
  }, [repoId, loadRepo, loadPulls]);

  useEffect(() => {
    if (!workspaceTarget || repo?.id !== workspaceTarget || tab !== "repositories") return;
    const frame = requestAnimationFrame(() => {
      document.getElementById("repo-workspace")?.scrollIntoView({ behavior: "smooth", block: "start" });
      setWorkspaceTarget(null);
    });
    return () => cancelAnimationFrame(frame);
  }, [workspaceTarget, repo?.id, tab]);

  useEffect(() => {
    if (!repoId || !branch) return;
    setFile(null); setTreeFiles([]); setScopeTree(null); setSyncSchedule(null);
    api<ScopeTreeData>(`/repositories/${repoId}/scope-tree?branch=${encodeURIComponent(branch)}`)
      .then(setScopeTree).catch(() => setScopeTree(null));
    api<Commit[]>(`/repositories/${repoId}/commits?branch=${encodeURIComponent(branch)}`)
      .then(rows => { setCommits(rows); setTccSha(rows[0]?.sha ?? ""); }).catch(() => setCommits([]));
    api<Record<string, boolean>>(`/repositories/${repoId}/access?branch=${encodeURIComponent(branch)}`)
      .then(setAccess).catch(() => setAccess({}));
    api<{ files: { path: string; size: number }[]; partial: boolean }>(`/repositories/${repoId}/tree?branch=${encodeURIComponent(branch)}`)
      .then(result => { setTreeFiles(result.files); setTreePartial(result.partial); })
      .catch(() => setTreeFiles([]));
    api<SyncSchedule>(`/repositories/${repoId}/sync-schedule?branch=${encodeURIComponent(branch)}`)
      .then(setSyncSchedule).catch(() => setSyncSchedule(null));
  }, [repoId, branch]);

  useEffect(() => {
    if (user.github_connected) {
      api<GitHubRepository[]>("/auth/github/repositories").then(setGithubRepos).catch(() => setGithubRepos([]));
    }
  }, [user.github_connected]);

  useEffect(() => {
    const choices = githubRepos.filter(r => r.can_push && !repositories.some(linked => linked.full_name === r.full_name));
    setChosenGithubRepo(current => choices.some(r => r.full_name === current) ? current : choices[0]?.full_name ?? "");
  }, [githubRepos, repositories]);

  useEffect(() => {
    if (tab === "activity" && projectId && project?.is_owner) {
      api<Audit[]>(`/projects/${projectId}/audit`).then(setAudit).catch(() => setAudit([]));
    }
  }, [tab, projectId, project?.is_owner]);

  async function act(fn: () => Promise<unknown>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); setNotice(message); }
    catch (problem) { setError(problem instanceof Error ? problem.message : "Request failed"); }
    finally { setBusy(false); }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    await act(async () => {
      const created = await api<Project>("/projects", "POST", { name: projectName });
      setProjectName(""); await loadProjects(created.id);
    }, "Project created.");
  }

  async function connectGithub(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/github/authorize", { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "GitHub authorization failed");
      window.location.assign(result.url);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "GitHub authorization failed");
      setBusy(false);
    }
  }

  async function linkRepo(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(chosenGithubRepo)) {
      setError("Enter a GitHub repository as owner/name."); return;
    }
    await act(async () => {
      const [github_owner, github_name] = chosenGithubRepo.split("/");
      const created = await api<Repository>(`/projects/${projectId}/repositories`, "POST", { github_owner, github_name });
      await loadProject(projectId); setRepoId(created.id); setChosenGithubRepo("");
    }, "Repository linked to project.");
  }

  async function linkRepoUrl(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      const created = await api<Repository>(`/projects/${projectId}/repositories/from-url`, "POST",
        { url: repoUrl.trim() });
      setRepoUrl(""); await loadProject(projectId); setRepoId(created.id);
      setTab("repositories"); setWorkspaceTarget(created.id);
    }, "Repository linked for viewing. Open its branch to browse files, activity, and memory.");
  }

  async function createGithubRepo(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      const created = await api<Repository>(`/projects/${projectId}/repositories/new`, "POST",
        { name: newRepoName, private: newRepoPrivate, description: "Created from DevHive" });
      setNewRepoName(""); await loadProject(projectId); setRepoId(created.id);
      setGithubRepos(await api<GitHubRepository[]>("/auth/github/repositories"));
    }, "GitHub repository created and linked.");
  }

  async function syncRepo() {
    if (!repoId) return;
    setBusy(true); setSyncing(true); setError(""); setNotice("");
    try {
      const response = await api<{ imported: number; indexed_files: number | null; file_chunks: number; partial: boolean }>(`/repositories/${repoId}/sync?branch=${encodeURIComponent(branch)}&limit=200`, "POST");
      setCommits(await api<Commit[]>(`/repositories/${repoId}/commits?branch=${encodeURIComponent(branch)}`));
      const updatedTree = await api<{ files: { path: string; size: number }[]; partial: boolean }>(
        `/repositories/${repoId}/tree?branch=${encodeURIComponent(branch)}`);
      setTreeFiles(updatedTree.files); setTreePartial(updatedTree.partial);
      api<ScopeTreeData>(`/repositories/${repoId}/scope-tree?branch=${encodeURIComponent(branch)}`)
        .then(setScopeTree).catch(() => undefined);
      if (file?.branch === branch) setFile(await api<FileResult>(
        `/repositories/${repoId}/file?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(file.path)}`));
      setSyncVersion(value => value + 1);
      api<SyncSchedule>(`/repositories/${repoId}/sync-schedule?branch=${encodeURIComponent(branch)}`)
        .then(setSyncSchedule).catch(() => undefined);
      setNotice(`${response.imported} new commits imported; ${response.indexed_files ?? 0} files and ${response.file_chunks} code sections searchable${response.partial ? " (partial snapshot: repository exceeded indexing limits)" : ""}.`);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Sync failed"); }
    finally { setBusy(false); setSyncing(false); }
  }

  async function previewFile(path: string, range?: { start: number; end: number } | null) {
    if (!repoId) return;
    setBusy(true); setError("");
    try {
      const loaded = await api<FileResult>(`/repositories/${repoId}/file?branch=${encodeURIComponent(branch)}&path=${encodeURIComponent(path)}`);
      setFile(loaded);
      setFileFocus(range ? { path, ...range } : null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not open file"); }
    finally { setBusy(false); }
  }

  // "Open in explorer" on an answer: jump to the exact file and lines in the Repositories tab.
  async function openFromAnswer(path: string, range?: { start: number; end: number } | null) {
    setTab("repositories");
    await previewFile(path, range);
    setTimeout(() => document.getElementById("file-review")?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
  }

  async function createBranch(event: FormEvent) {
    event.preventDefault();
    if (!repoId) return;
    await act(async () => {
      await api(`/repositories/${repoId}/branches`, "POST", { source: branch, name: newBranchName });
      const rows = await api<{ name: string }[]>(`/repositories/${repoId}/branches`);
      setBranches(rows.map(row => row.name)); setBranch(newBranchName); setNewBranchName("");
    }, "Branch created on GitHub.");
  }

  async function createPull(event: FormEvent) {
    event.preventDefault();
    if (!repoId) return;
    await act(async () => {
      await api(`/repositories/${repoId}/pulls`, "POST", { head: branch, base: pullBase,
        title: pullTitle, body: "" });
      setPullTitle(""); await loadPulls(repoId);
    }, "Pull request opened on GitHub.");
  }

  async function mergePull(number: number, base: string) {
    if (!repoId) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api(`/repositories/${repoId}/pulls/${number}/merge`, "PUT");
      await loadPulls(repoId);
      try {
        await api(`/repositories/${repoId}/sync?branch=${encodeURIComponent(base)}&limit=10`, "POST");
        if (base === branch) setCommits(await api<Commit[]>(`/repositories/${repoId}/commits?branch=${encodeURIComponent(base)}`));
        setNotice("Pull request merged and base branch memory updated.");
      } catch { setNotice("Pull request merged. Sync the base branch to update memory."); }
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Merge failed"); }
    finally { setBusy(false); }
  }

  async function saveTCC(event: FormEvent) {
    event.preventDefault();
    if (!repoId) return;
    await act(async () => {
      await api(`/repositories/${repoId}/tcc`, "POST", { branch, sha: tccSha, thought, change_summary: changeSummary, comment });
      setThought(""); setChangeSummary(""); setComment("");
    }, "TCC saved to engineering memory.");
  }

  async function saveMember(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      await api(`/projects/${projectId}/members`, "PUT", { email: memberEmail, role_id: memberRoleId });
      setMemberEmail(""); await loadProject(projectId);
    }, "Member role saved. Project repositories are now visible to this member.");
  }

  async function inviteMember(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      await api(`/projects/${projectId}/invitations`, "POST", {
        email: inviteEmail, role_id: inviteRoleId,
      });
      setInviteEmail("");
      setInvitations(await api<Invitation[]>(`/projects/${projectId}/invitations`));
    }, "Invitation email sent. Access begins after the recipient signs in.");
  }

  async function cancelInvitation(id: string) {
    if (!projectId) return;
    await act(async () => {
      await api(`/projects/${projectId}/invitations/${id}`, "DELETE");
      setInvitations(await api<Invitation[]>(`/projects/${projectId}/invitations`));
    }, "Invitation canceled.");
  }

  function editRole(role?: Role) {
    setEditingRoleId(role?.id ?? null); setRoleName(role?.name ?? "");
    setRoleColor(role?.color ?? "#64748b"); setRolePermissions(role?.permissions ?? []);
  }

  async function saveRole(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      const path = `/projects/${projectId}/roles${editingRoleId ? `/${editingRoleId}` : ""}`;
      await api(path, editingRoleId ? "PUT" : "POST", {
        name: roleName, color: roleColor, permissions: rolePermissions, position: 20,
      });
      editRole(); await loadProject(projectId);
    }, "Role permissions saved.");
  }

  async function saveOverride(event: FormEvent) {
    event.preventDefault();
    if (!projectId) return;
    await act(async () => {
      await api(`/projects/${projectId}/branch-overrides`, "PUT", {
        role_id: overrideRoleId, pattern: overridePattern, allow: overrideAllow, deny: overrideDeny,
      });
      await loadProject(projectId);
    }, "Branch rule saved.");
  }

  function toggle(name: string, list: string[], setter: (value: string[]) => void) {
    setter(list.includes(name) ? list.filter(item => item !== name) : [...list, name]);
  }

  async function logout() {
    await supabaseBrowser()?.auth.signOut().catch(() => undefined);
    await fetch("/api/session/logout", { method: "POST" });
    window.location.assign("/login");
  }

  const owner = project?.is_owner ?? false;
  const canCreateRepo = project?.permissions?.includes("create_repository") ?? false;
  const availableGithubRepos = githubRepos.filter(r => r.can_push && !repositories.some(linked => linked.full_name === r.full_name));
  const supabaseEnabled = Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL &&
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY);

  return <div className="app-shell">
    <aside className="sidebar" style={{ width: sidebarWidth, flexBasis: sidebarWidth }}>
      <div className="brand"><span className="brand-mark">D</span><span>DevHive<small>WORKSPACE</small></span></div>
      <div className="sidebar-label">YOUR PROJECTS</div>
      <nav className="project-nav" aria-label="Projects">
        {projects.map(p => <button key={p.id} className={`project-link ${projectId === p.id ? "active" : ""}`}
          onClick={() => { setProjectId(p.id); setTab("repositories"); }}>
          <span className="project-icon">{p.name.slice(0, 1).toUpperCase()}</span><span>{p.name}</span>
        </button>)}
      </nav>
      <form className="sidebar-create" onSubmit={createProject}>
        <input aria-label="New project name" placeholder="New project name" value={projectName}
          onChange={e => setProjectName(e.target.value)} required minLength={2} />
        <button className="button button-sidebar" disabled={busy}>+ Create project</button>
      </form>
      <div className="sidebar-bottom"><div className="avatar">{user.name.slice(0, 1).toUpperCase()}</div>
        <div className="sidebar-user"><strong>{user.name}</strong><small>{user.email}</small></div>
        <button className="icon-button" title="Sign out" aria-label="Sign out" onClick={logout}>↗</button></div>
      <div className="sidebar-resizer" role="separator" aria-label="Resize sidebar" aria-orientation="vertical"
        onPointerDown={event => { dragStart.current = { x: event.clientX, width: sidebarWidth }; event.currentTarget.setPointerCapture(event.pointerId); }} />
    </aside>

    <main className={`main-area ${tab === "memory" ? "main-area--fill" : ""}`}>
      <header className="topbar"><div><span className="crumb">Workspace</span><span className="crumb-separator">/</span>
        <strong>{project?.name ?? "Overview"}</strong></div>
        <div className="account-actions"><button className="text-button" onClick={() => { setTab("settings"); setSettingsSection("connections"); }}>Settings</button></div>
      </header>
      <div className={`main-content ${tab === "memory" ? "memory-main" : ""}`}>
        {error && <div className="message message-error" role="alert">{error}<button onClick={() => setError("")}>×</button></div>}
        {notice && <div className="message message-success" role="status">{notice}<button onClick={() => setNotice("")}>×</button></div>}
        {githubStatus?.connected && <div className="message message-success" role="status">GitHub connected. Your repositories are ready to choose.</div>}
        {githubStatus?.error && <div className="message message-error" role="alert">GitHub connection failed: {githubStatus.error.replaceAll("_", " ")}</div>}

        {!project ? <section className="empty-welcome"><p className="eyebrow">GET STARTED</p>
          <h1>Your engineering home starts here.</h1>
          <p>Create a project in the sidebar. Then paste a public GitHub link or connect GitHub to add a private repository.</p></section> : <>
          {tab !== "memory" && <div className="page-heading"><div><p className="eyebrow">PROJECT SPACE</p><h1>{project.name}</h1>
            <p>{repositories.length} visible repositor{repositories.length === 1 ? "y" : "ies"} · {members.length} team member{members.length === 1 ? "" : "s"}</p></div>
            {owner && <span className="owner-badge">Project owner</span>}</div>}
          <div className="tabs" role="tablist">
            {([ ["repositories", "Repositories"], ["activity", "Activity"], ["memory", "Memory"],
              ["settings", "Settings"] ] as [Tab, string][]).map(([key, label]) =>
              <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? "selected" : ""}
                onClick={() => setTab(key)}>{label}</button>)}
          </div>

          {tab === "repositories" && <div className="repo-page">
            <section className="column-primary">
              <div className="section-heading"><div><p className="eyebrow">SOURCE</p><h2>Project repositories</h2></div>
                <div className="repo-heading-actions"><span className="count-badge">{repositories.length}</span>
                  {canCreateRepo && <button className="button button-secondary" onClick={() => {
                    setTab("settings"); setSettingsSection("connections");
                  }}>Add GitHub link</button>}</div></div>
              {repositories.length ? <div className="repo-grid">{repositories.map(r =>
                <button key={r.id} className={`repo-card ${repoId === r.id ? "selected" : ""}`} onClick={() => {
                  setRepoId(r.id); setTab("repositories"); setWorkspaceTarget(r.id);
                }}>
                  <span className="repo-glyph">⌘</span><strong>{r.github_name ?? r.full_name.split("/")[1]}</strong>
                  <small>{r.full_name}{r.read_only ? " · View only" : ""}</small><span className="repo-card-bottom">Open workspace <span>→</span></span>
                </button>)}</div> : <div className="empty-card">No repositories have been linked to this project yet.</div>}

              {repo && <div className="panel repo-detail" id="repo-workspace">
                <div className="section-heading"><div><p className="eyebrow">REPOSITORY WORKSPACE</p><h2>{repo.full_name}</h2></div>
                  <a className="text-link" href={repo.html_url} target="_blank" rel="noreferrer">Open on GitHub ↗</a></div>
                {repo.read_only && <p className="hint">Linked by URL · View only. Browse branches and files, review activity, and sync memory.</p>}
                <div className="toolbar"><label>Branch <select value={branch} onChange={e => setBranch(e.target.value)}>
                  {!branches.includes(branch) && <option value={branch}>{branch}</option>}
                  {branches.map(name => <option key={name} value={name}>{name}</option>)}
                </select></label>
                  {access.sync_repository && <button className="button button-secondary" disabled={busy} onClick={syncRepo}>{syncing ? "Syncing…" : "Sync memory"}</button>}</div>
                {syncing && <p className="sync-progress" role="status"><span className="spinner" aria-hidden="true"/>Importing commit history and indexing code. The first sync of a repository can take a minute or two; later syncs only process what changed.</p>}
                {syncSchedule && <div className="sync-schedule"><div><strong>{syncSchedule.enabled ?
                  `Automatic sync · ${syncSchedule.day} ${syncSchedule.local_time} IST` : "Automatic sync is disabled"}</strong>
                  <small>{syncSchedule.enabled && syncSchedule.next_run ?
                    `Next: ${new Date(syncSchedule.next_run).toLocaleString()}` : "Use Sync memory for a manual refresh"}</small></div>
                  <span>{syncSchedule.last_indexed_at ?
                    `Last indexed ${new Date(syncSchedule.last_indexed_at).toLocaleString()}` : "This branch has not been indexed yet"}</span></div>}
                {access.create_branch && <form className="inline-form" onSubmit={createBranch}>
                  <input value={newBranchName} onChange={e => setNewBranchName(e.target.value)} placeholder="feature/my-work" aria-label="New branch name" required />
                  <button className="button button-secondary" disabled={busy}>Create branch from {branch}</button></form>}
                <div className="subheading">Recent commits <span>{commits.length}</span></div>
                {commits.length ? <div className="commit-list">{commits.map(c => <div className="commit-row" key={c.id}>
                  <span className="timeline-dot"/><div><strong>{c.message.split("\n")[0]}</strong>
                  <small>{c.author} · {readableDate(c.committed_at)} · {c.files.length} files</small></div>
                  <code>{shortSha(c.sha)}</code></div>)}</div> : <p className="muted">No commits imported for this branch. Choose Sync memory to index commits and files.</p>}
                <FileReview key={`${repo.id}:${branch}:${syncVersion}`} repoId={repo.id} branch={branch} files={treeFiles} partial={treePartial}
                  file={file} onSelect={previewFile} focus={fileFocus}/>
                <details className="pull-panel"><summary>Pull requests <span className="count-badge">{pulls.length} open</span></summary>
                <p className="muted">Pull requests propose merging work from a feature branch into another branch, usually main. This section shows open GitHub proposals and lets permitted members open or merge them.</p>
                {pulls.map(pull => <div className="activity-item" key={pull.number}>
                  <span className="timeline-dot"/><div><strong>#{pull.number} · {pull.title}</strong>
                    <small>{pull.author} · {pull.head} → {pull.base}</small></div>
                  {pull.can_merge ? <button className="button button-secondary" disabled={busy}
                    onClick={() => mergePull(pull.number, pull.base)}>Merge</button> :
                    <a className="text-link" href={pull.url} target="_blank" rel="noreferrer">View</a>}
                </div>)}
                {!pulls.length && <p className="muted">No open pull requests for this repository.</p>}
                {access.push && branch !== repo.default_branch && <form className="stack" onSubmit={createPull}>
                  <label>Open a pull request from {branch}<input value={pullTitle} onChange={e => setPullTitle(e.target.value)}
                    minLength={3} placeholder="What changed?" required /></label>
                  <label>Into branch<select value={pullBase} onChange={e => setPullBase(e.target.value)}>
                    {branches.filter(name => name !== branch).map(name => <option key={name} value={name}>{name}</option>)}
                  </select></label><button className="button button-secondary" disabled={busy || !pullBase || pullBase === branch}>Open pull request</button>
                </form>}
                {branch === repo.default_branch && <p className="hint">Select a feature branch to propose changes into {repo.default_branch}.</p>}
                </details>
              </div>}
            </section>

          </div>}

          {tab === "settings" && <><div className="settings-navigation">
            {([ ["connections", "Connections & repositories"], ["people", "People & access"],
              ["permissions", "Roles & branch rules"] ] as const).map(([key, label]) =>
              <button className={`button ${settingsSection === key ? "button-primary" : "button-secondary"}`} key={key}
                onClick={() => setSettingsSection(key)}>{label}</button>)}
          </div>
          {settingsSection === "connections" && <div className="two-column"><section className="column-primary">
            {canCreateRepo && <div className="panel"><p className="eyebrow">REPOSITORY SOURCE</p><h3>Link a repository</h3>
              <p className="muted">Paste a GitHub link to view someone else's public repository. Private repositories require read access through your connected GitHub account.</p>
              <form className="stack" onSubmit={linkRepoUrl}><label>GitHub repository link<input type="url"
                value={repoUrl} onChange={e => setRepoUrl(e.target.value)}
                placeholder="https://github.com/owner/repository" required /></label>
                <button className="button button-primary" disabled={busy || !repoUrl.trim()}>Link for viewing</button></form>
              <div className="divider"/><h3>Choose from your GitHub account</h3>
              <p className="muted">Repositories where you can push appear here automatically.</p>
              <form className="stack" onSubmit={linkRepo}><label>Choose a repository<select
                value={chosenGithubRepo} onChange={e => setChosenGithubRepo(e.target.value)} required>
                <option value="">{user.github_connected ? "No unlinked repositories available" : "Connect GitHub first"}</option>
                {availableGithubRepos.map(r => <option key={r.full_name} value={r.full_name}>{r.full_name}</option>)}
              </select></label>
                <button className="button button-primary" disabled={busy || !chosenGithubRepo}>Link repository</button></form>
              <div className="divider"/><h3>Create on GitHub</h3>
              <form className="stack" onSubmit={createGithubRepo}><label>Name<input value={newRepoName}
                onChange={e => setNewRepoName(e.target.value)} placeholder="new-repository" required /></label>
                <label className="check-line"><input type="checkbox" checked={newRepoPrivate} onChange={e => setNewRepoPrivate(e.target.checked)} /> Private repository</label>
                <button className="button button-secondary" disabled={busy || !user.github_connected}>Create repository</button></form>
            </div>}
            <div className="panel"><p className="eyebrow">ACCOUNT CONNECTION</p><h2>GitHub</h2>
              <p className="muted">{user.github_connected ? `Connected as ${user.github_login}.` : "Authorize your GitHub account to discover repositories and link private repositories."} Your token stays on the server.</p>
              <form onSubmit={connectGithub}><button className="button button-primary" disabled={busy}>{user.github_connected ? "Reconnect GitHub" : "Authorize GitHub"}</button></form>
            </div>
            <GroqSettings />
          </section><aside className="column-side"><div className="panel"><p className="eyebrow">SIGN-IN PROVIDERS</p><h3>Linked sign-in</h3>
            <p className="muted">Link a provider to sign in to DevHive. GitHub repository access uses the separate authorization above.</p>
            {supabaseEnabled ? <div className="stack"><button className="button button-secondary" onClick={() => startSupabaseSignIn("github", true).catch(e => setError(e.message))}>Link GitHub login</button>
              <button className="button button-secondary" onClick={() => startSupabaseSignIn("google", true).catch(e => setError(e.message))}>Link Google login</button></div> :
              <p className="hint">Configure Supabase Auth to link sign-in providers.</p>}
          </div></aside></div>}
          </>}

          {tab === "memory" && !repo && <div className="empty-card">Choose a repository from Repositories first.</div>}
          {/* Stays mounted while another tab is open, so the conversation and any running question survive. */}
          {repo && <div className="memory-host" hidden={tab !== "memory"}>
            <MemoryWorkspace key={repo.id} repoId={repo.id} repoName={repo.full_name} branch={branch} onOpenFile={openFromAnswer}
              scopeFiles={scopeTree?.files ?? []} scopeReady={scopeTree?.profile_ready ?? true}
              embeddingMode={syncSchedule?.embedding_mode ?? "lexical"}
              needsReindex={syncSchedule?.needs_reindex ?? false}
              selectedPath={file?.branch === branch ? file.path : undefined} sideContent={
              access.write_tcc && access.push ? <details className="memory-capture"><summary>Record a commit thought</summary><div>
                <p className="muted">Add one permanent Thought, Change and Comment record to an imported commit.</p>
                <form className="stack" onSubmit={saveTCC}><label>Commit<select value={tccSha} onChange={e => setTccSha(e.target.value)} required>
                  {commits.map(c => <option key={c.id} value={c.sha}>{shortSha(c.sha)} · {c.message.split("\n")[0]}</option>)}
                </select></label><label>Thought<textarea value={thought} onChange={e => setThought(e.target.value)} required /></label>
                  <label>Change summary<textarea value={changeSummary} onChange={e => setChangeSummary(e.target.value)} required /></label>
                  <label>Comment<textarea value={comment} onChange={e => setComment(e.target.value)} required /></label>
                  <button className="button button-secondary" disabled={busy || !commits.length}>Save TCC</button></form>
              </div></details> : undefined} />
          </div>}

          {tab === "settings" && settingsSection === "people" && <div className="two-column"><section className="column-primary">
            <div className="section-heading"><div><p className="eyebrow">COLLABORATION</p><h2>Project members</h2></div><span className="count-badge">{members.length}</span></div>
            <div className="panel member-list">{members.map(m => <div className="member-row" key={m.user_id}>
              <span className="avatar avatar-small">{m.name.slice(0, 1).toUpperCase()}</span>
              <div><strong>{m.name}</strong><small>{m.email}</small></div>
              <span className="role-pill">{m.role_name}</span>
              {owner && !m.is_owner && <button className="text-button danger" onClick={() => act(async () => {
                await api(`/projects/${projectId}/members/${m.user_id}`, "DELETE"); await loadProject(projectId!);
              }, "Member removed.")}>Remove</button>}
            </div>)}</div>
            <div className="panel"><p className="eyebrow">REPOSITORY VISIBILITY</p>
              <p className="muted">Every project member can read linked repositories, branches, activity, and memory. The owner sets write permissions through roles and branch rules.</p>
            </div>
          </section><aside className="column-side">{owner && <div className="panel"><p className="eyebrow">OWNER CONTROL</p><h3>Add a member</h3>
            <p className="muted">The person must create a DevHive account first.</p>
            <form className="stack" onSubmit={saveMember}><label>Email<input type="email" value={memberEmail}
              onChange={e => setMemberEmail(e.target.value)} required /></label>
              <label>Role<select value={memberRoleId} onChange={e => setMemberRoleId(e.target.value)} required>
                <option value="">Choose a role</option>{roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
              </select></label><button className="button button-primary" disabled={busy}>Save member</button></form></div>}
            {owner && <div className="panel"><p className="eyebrow">SUPABASE INVITE</p><h3>Invite a new teammate</h3>
              <p className="muted">Supabase emails an invitation. After sign-in, the teammate can read every repository linked to this project.</p>
              <form className="stack" onSubmit={inviteMember}><label>Email<input type="email" value={inviteEmail}
                onChange={e => setInviteEmail(e.target.value)} required /></label>
                <label>Role<select value={inviteRoleId} onChange={e => setInviteRoleId(e.target.value)} required>
                  <option value="">Choose a role</option>{roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select></label>
                <button className="button button-primary" disabled={busy || !supabaseEnabled}>Send invitation</button>
              </form>
              {!supabaseEnabled && <p className="hint">Configure Supabase Auth to enable email invitations.</p>}
              {invitations.map(i => <div className="grant-row" key={i.id}><span>{i.email}<small>Pending invitation</small></span>
                <button className="button button-secondary" onClick={() => cancelInvitation(i.id)}>Cancel</button></div>)}
            </div>}</aside></div>}

          {tab === "settings" && settingsSection === "permissions" && <div className="two-column"><section className="column-primary">
            <div className="section-heading"><div><p className="eyebrow">ACCESS CONTROL</p><h2>Roles and branch rules</h2></div></div>
            <div className="panel"><h3>Project roles</h3><p className="muted">The project owner always has full access. Other roles can be edited only by the owner.</p>
              {roles.map(r => <div className="role-row" key={r.id}><span className="role-dot" style={{ background: r.color }}/>
                <div><strong>{r.name}</strong><small>{r.permissions.join(" · ") || "Read-only"}</small></div>
                {owner && <button className="text-button" onClick={() => editRole(r)}>Edit</button>}</div>)}
            </div><div className="panel"><h3>Branch overrides</h3>
              {overrides.length ? overrides.map(rule => <div className="override-row" key={rule.id}>
                <code>{rule.pattern}</code><span>{roles.find(r => r.id === rule.role_id)?.name ?? "Role"}</span>
                <small>{rule.allow.length ? `Allow: ${rule.allow.join(", ")}` : ""} {rule.deny.length ? `Deny: ${rule.deny.join(", ")}` : ""}</small></div>) : <p className="muted">No branch overrides.</p>}</div>
          </section><aside className="column-side">{owner && <><div className="panel"><p className="eyebrow">OWNER ONLY</p><h3>{editingRoleId ? "Edit role" : "Create role"}</h3>
            <form className="stack" onSubmit={saveRole}><label>Role name<input value={roleName} onChange={e => setRoleName(e.target.value)} required /></label>
              <label>Role color<input type="color" value={roleColor} onChange={e => setRoleColor(e.target.value)} /></label>
              <div className="checkbox-grid">{permissionCatalog.map(name => <label key={name} className="check-line"><input type="checkbox"
                checked={rolePermissions.includes(name)} onChange={() => toggle(name, rolePermissions, setRolePermissions)} />{name.replaceAll("_", " ")}</label>)}</div>
              <button className="button button-primary" disabled={busy}>{editingRoleId ? "Save role" : "Create role"}</button>
              {editingRoleId && <button type="button" className="text-button" onClick={() => editRole()}>Cancel edit</button>}</form></div>
            <div className="panel"><p className="eyebrow">BRANCH CONTROL</p><h3>Set an override</h3>
              <form className="stack" onSubmit={saveOverride}><label>Role<select value={overrideRoleId} onChange={e => setOverrideRoleId(e.target.value)} required>
                <option value="">Choose a role</option>{roles.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}</select></label>
                <label>Branch pattern<input value={overridePattern} onChange={e => setOverridePattern(e.target.value)} required placeholder="feature/*" /></label>
                <span className="field-caption">Allow on matching branches</span>
                <div className="checkbox-grid">{permissionCatalog.filter(name => BRANCH_PERMISSIONS.has(name)).map(name => <label className="check-line" key={name}><input type="checkbox"
                  checked={overrideAllow.includes(name)} disabled={overrideDeny.includes(name)}
                  onChange={() => toggle(name, overrideAllow, setOverrideAllow)} />{name.replaceAll("_", " ")}</label>)}</div>
                <span className="field-caption">Deny on matching branches</span>
                <div className="checkbox-grid">{permissionCatalog.filter(name => BRANCH_PERMISSIONS.has(name)).map(name => <label className="check-line" key={name}><input type="checkbox"
                  checked={overrideDeny.includes(name)} disabled={overrideAllow.includes(name)}
                  onChange={() => toggle(name, overrideDeny, setOverrideDeny)} />{name.replaceAll("_", " ")}</label>)}</div>
                <button className="button button-secondary" disabled={busy}>Save branch rule</button></form></div></>}</aside></div>}

          {tab === "activity" && <><div className="section-heading"><div>
            <p className="eyebrow">CHANGE TRAIL</p><h2>Repository activity</h2></div></div>
            {repo ? <GitHubActivity key={repo.id} repoId={repo.id} branches={branches} branch={branch} /> :
              <div className="empty-card">Choose a repository first.</div>}
            {owner && <div className="panel audit-panel"><h3>DevHive access audit</h3>{audit.map(event => <div className="audit-row" key={event.id}>
              <span className={`audit-status ${event.allowed ? "" : "denied"}`}>{event.allowed ? "✓" : "×"}</span>
              <div><strong>{event.action.replaceAll("_", " ")}</strong><small>{event.target}</small></div>
              <time>{readableDate(event.created_at)}</time></div>)}
              {!audit.length && <p className="muted">No recorded activity yet.</p>}</div>}
          </>}
        </>}
      </div>
    </main>
  </div>;
}
