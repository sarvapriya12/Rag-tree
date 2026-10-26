"use client";

import { CSSProperties, FormEvent, KeyboardEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import AnswerMarkdown from "@/components/AnswerMarkdown";
import KnowledgePane from "@/components/KnowledgePane";
import ScopeTree from "@/components/ScopeTree";
import { DEPTHS, DEPTH_LABEL, isDepth } from "@/lib/depth";
import { useResizable } from "@/lib/panes";
import { covered, normalize } from "@/lib/scope";
import type { Answer, ChatJob, Depth, LlmSettings, ScopeFile } from "@/types";

type Exchange = { question: string; answer: Answer };
type OpenFile = (path: string, range?: { start: number; end: number } | null) => void;
type JobView = { stage: string; step: number; total: number; message: string; elapsed: number };

const EXAMPLES = ["What does each folder of this repository do?", "Where is the main entry point and what does it do?",
  "Who has contributed to this repository, and when?", "Which files handle configuration or setup?"];
const MAX_QUESTION = 300;
const POLL_MS = 1500;

function statusNote(answer: Answer, llm: LlmSettings | null) {
  // With nothing retrieved there is nothing to explain, so the AI status would only confuse.
  if (!answer.files?.length && !answer.citations?.length && !answer.snippets?.length && !answer.team?.length) return null;
  switch (answer.llm_status) {
    case "ok": return { tone: "ok", text: `Explained by ${llm?.model ?? "Groq"} from the evidence at right. Verify against the code.` };
    case "partial": return { tone: "warn", text: "The final write-up could not finish, so this shows the per-folder summaries instead." };
    case "rate_limited": return { tone: "warn", text: "The AI is busy (its shared rate limit was reached). This is a summary of the retrieved evidence; ask again in a minute for a full explanation." };
    case "unavailable": return { tone: "warn", text: "The AI could not answer just now. Showing a summary of the retrieved evidence instead." };
    case "no_safe_evidence": return { tone: "warn", text: "Nothing safe to send to the AI was found (secrets are never sent). Showing retrieved evidence instead." };
    case "not_configured": return { tone: "warn", text: "No AI key is configured, so this is an extractive summary. Add a Groq key in Settings to get explanations." };
    default: return null;
  }
}

function jobLabel(job: JobView) {
  if (job.stage === "map") return `Summarising ${job.message} (${job.step + 1} of ${job.total})`;
  if (job.stage === "reduce") return "Writing the final answer from the summaries";
  if (job.stage === "retrieving") return "Searching the index";
  return job.message || "Starting";
}

function jobPercent(job: JobView) {
  if (job.stage === "reduce") return 90;
  if (job.stage === "map") return 12 + Math.round(72 * (job.step / Math.max(1, job.total)));
  return job.stage === "retrieving" ? 6 : 3;
}

function DepthControl({ value, onChange, disabled }: { value: Depth; onChange: (depth: Depth) => void; disabled: boolean }) {
  return <div className="depth" role="radiogroup" aria-label="Answer depth">
    {DEPTHS.map(option => <button key={option.key} type="button" role="radio" aria-checked={value === option.key}
      disabled={disabled} className={`depth__opt ${value === option.key ? "depth__opt--on" : ""}`}
      title={option.detail} onClick={() => onChange(option.key)}>{option.label}</button>)}
  </div>;
}

export default function MemoryWorkspace({ repoId, repoName, branch, scopeFiles, scopeReady, selectedPath, sideContent,
  embeddingMode, needsReindex, onOpenFile }: {
  repoId: string; repoName: string; branch: string; scopeFiles: ScopeFile[]; scopeReady: boolean;
  selectedPath?: string; sideContent?: ReactNode;
  embeddingMode: "semantic" | "lexical"; needsReindex: boolean; onOpenFile?: OpenFile;
}) {
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState<string[]>([]);
  const [depth, setDepth] = useState<Depth>("medium");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [selected, setSelected] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState<JobView | null>(null);
  const [error, setError] = useState("");
  const [llm, setLlm] = useState<LlmSettings | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [pane, setPane] = useState<"chat" | "knowledge">("chat");
  const threadEnd = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const alive = useRef(true);
  const cancelled = useRef(false);
  const jobId = useRef<string | null>(null);
  const tree = useResizable("devhive_ask_tree_w", 280, 200, 480, 1);
  const knowledge = useResizable("devhive_ask_knowledge_w", 440, 320, 760, -1);
  const index = Math.min(selected, exchanges.length - 1);
  const current = exchanges[index]?.answer ?? null;
  const hits = useMemo(() => new Set((current?.files ?? []).map(file => file.path)), [current]);
  const depthInfo = DEPTHS.find(option => option.key === depth)!;

  useEffect(() => { setExchanges([]); setSelected(0); setScope([]); setError(""); }, [repoId, branch]);
  useEffect(() => { api<LlmSettings>("/auth/llm").then(setLlm).catch(() => undefined); }, []);
  useEffect(() => {
    try { const saved = localStorage.getItem("devhive_ask_depth"); if (isDepth(saved)) setDepth(saved); } catch { /* optional */ }
  }, []);
  useEffect(() => { threadEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [exchanges.length, busy]);
  useEffect(() => {
    const box = input.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 168)}px`;
  }, [question]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (jobId.current) api(`/repositories/${repoId}/chat/jobs/${jobId.current}`, "DELETE").catch(() => undefined);
    };
  }, [repoId]);

  function chooseDepth(next: Depth) {
    setDepth(next);
    try { localStorage.setItem("devhive_ask_depth", next); } catch { /* optional */ }
  }

  function cite(label: string, at: number) {
    setSelected(at);
    setFlash(label);
    setPane("knowledge");
    // The knowledge pane re-renders for the chosen exchange before the card can be found.
    setTimeout(() => {
      const card = document.getElementById(`ev-${at}-${label}`);
      card?.closest("details")?.setAttribute("open", "");
      card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 40);
    setTimeout(() => setFlash(null), 1800);
  }

  async function runJob(body: object): Promise<Answer | null> {
    const started = await api<{ job_id: string }>(`/repositories/${repoId}/chat/jobs`, "POST", body);
    jobId.current = started.job_id;
    setJob({ stage: "queued", step: 0, total: 0, message: "Waiting to start", elapsed: 0 });
    let failures = 0;
    try {
      while (alive.current && !cancelled.current) {
        await new Promise(resolve => setTimeout(resolve, POLL_MS));
        if (!alive.current || cancelled.current) break;
        let state: ChatJob;
        try {
          state = await api<ChatJob>(`/repositories/${repoId}/chat/jobs/${started.job_id}`);
          failures = 0;
        } catch (problem) {
          failures += 1;
          if (failures >= 3 || (problem instanceof Error && /not found/i.test(problem.message))) throw problem;
          continue;
        }
        setJob({ stage: state.stage, step: state.step, total: state.total, message: state.message, elapsed: state.elapsed });
        if (state.status === "done" && state.result) return state.result;
        if (state.status === "error") throw new Error(state.error || "The question could not be answered");
        if (state.status === "cancelled") return null;
      }
      return null;
    } finally { jobId.current = null; }
  }

  function cancelJob() {
    cancelled.current = true;
    const id = jobId.current;
    if (id) api(`/repositories/${repoId}/chat/jobs/${id}`, "DELETE").catch(() => undefined);
    setJob(null);
  }

  async function ask(event?: FormEvent) {
    event?.preventDefault();
    const message = question.trim();
    if (message.length < 2 || !branch || busy) return;
    setBusy(true); setError(""); cancelled.current = false;
    const body = { message, branch, depth, scope };
    try {
      const answer = depth === "very_high" ? await runJob(body) : await api<Answer>(`/repositories/${repoId}/chat`, "POST", body);
      if (answer && alive.current) {
        setExchanges(rows => [...rows, { question: message, answer }]);
        setSelected(exchanges.length);
        setQuestion("");
      }
    } catch (problem) {
      if (alive.current) setError(problem instanceof Error ? problem.message : "Memory lookup failed");
    } finally {
      if (alive.current) { setBusy(false); setJob(null); }
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask(); }
  }

  const aiLine = llm?.active ? `AI explanations on · ${llm.model}${llm.configured ? " · your key" : " · workspace key"}`
    : llm ? "AI explanations off · add a Groq key in Settings" : "";
  const openFileName = selectedPath?.split("/").pop();
  const offerOpenFile = selectedPath && !covered(selectedPath, scope);

  return <section className={`ask ${drawer ? "ask--drawer" : ""}`} data-pane={pane} aria-label="Engineering memory"
    style={{ "--ask-tree": `${tree.width}px`, "--ask-knowledge": `${knowledge.width}px` } as CSSProperties}>
    <aside className="ask__scope" aria-label="Scope">
      <ScopeTree files={scopeFiles} selected={scope} onChange={setScope} hits={hits} ready={scopeReady}
        onClose={() => setDrawer(false)} />
    </aside>
    <div className="ask__splitter ask__splitter--tree" aria-label="Resize the scope panel" {...tree.handle} />

    <div className="ask__chat">
      <header className="ask__head">
        <div><p className="eyebrow">ENGINEERING MEMORY</p><h2>Ask your codebase</h2>
          <p className="ask__where">{repoName} <span>·</span> {branch || "Choose a branch"}</p></div>
        <div className="ask__head-tools">
          <button type="button" className="ask__scope-toggle button button-secondary" onClick={() => setDrawer(true)}>
            Scope{scope.length ? ` (${scope.length})` : ""}</button>
          <div className="ask__pane-switch" role="tablist" aria-label="Conversation or retrieved knowledge">
            <button type="button" role="tab" aria-selected={pane === "chat"} onClick={() => setPane("chat")}>Conversation</button>
            <button type="button" role="tab" aria-selected={pane === "knowledge"} onClick={() => setPane("knowledge")}>Knowledge</button>
          </div>
          <span className="memory-mode" title="Search combines meaning (embeddings) with exact BM25 keyword matching">
            {embeddingMode === "semantic" ? "Semantic + BM25" : "BM25 keyword"}</span>
        </div>
      </header>

      <div className="ask__thread" role="log" aria-live="polite">
        {needsReindex && <div className="memory-reindex">This branch was indexed with an older version. Choose Sync memory in Repositories to rebuild it, then folder selection and deeper answers will work best.</div>}
        {exchanges.length === 0 && !busy && <div className="memory-empty"><span className="memory-empty-mark">✦</span>
          <h3>Find the work behind the code.</h3>
          <p>Ask what a feature does, where it lives, who changed it and when. Pick folders on the left to focus the search, and a depth below to choose how thorough the answer is. Every answer links to the exact file, lines and commits.</p>
          <div className="memory-examples">
            {EXAMPLES.map(example => <button type="button" key={example} onClick={() => { setQuestion(example); input.current?.focus(); }}>{example}</button>)}
          </div></div>}
        {exchanges.map((exchange, at) => {
          const note = statusNote(exchange.answer, llm);
          const active = at === index;
          const places = exchange.answer.files?.length ?? exchange.answer.snippets?.length ?? 0;
          const stats = exchange.answer.retrieval;
          const used = exchange.answer.depth;
          return <div className={`memory-exchange ${active ? "active" : ""}`} key={at}>
            <div className="memory-user-question"><span>You</span><p>{exchange.question}</p>
              <small>{used ? `${DEPTH_LABEL[used]} depth` : ""}{exchange.answer.scope?.length ? ` · ${exchange.answer.scope.length} in scope` : " · whole repository"}</small></div>
            <div className="memory-answer" onClick={() => setSelected(at)}>
              <span>{exchange.answer.llm_status === "ok" || exchange.answer.llm_status === "partial" ?
                `AI explanation${used ? ` · ${DEPTH_LABEL[used]}` : ""}` :
                exchange.answer.tool_used === "code_lookup" ? "Code lookup" :
                exchange.answer.tool_used === "inspect_file" ? "File review" : "Memory search"}</span>
              <AnswerMarkdown text={exchange.answer.answer} onCite={label => cite(label, at)}/>
              <div className="answer-foot">
                <small>{places} code location{places === 1 ? "" : "s"}
                  {stats ? ` in ${stats.folders_covered} folder${stats.folders_covered === 1 ? "" : "s"}` : ""}
                  {" · "}{exchange.answer.citations?.filter(c => c.kind !== "file").length ?? 0} commit sources
                  {active ? " · shown at right" : " · click to show at right"}</small>
                {note && <small className={`status-note ${note.tone}`}>{note.text}</small>}</div>
            </div></div>;
        })}
        {busy && !job && <div className="memory-answer thinking" aria-label="Searching">
          <span>Searching</span><p>Searching code, commits and contributors<i>.</i><i>.</i><i>.</i></p>
          <div className="skeleton"><b/><b/><b/></div></div>}
        {busy && job && <div className="memory-answer job-progress" role="status" aria-live="polite">
          <span>Very high · in progress</span>
          <p>{jobLabel(job)}</p>
          <div className="job-progress__bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={jobPercent(job)}>
            <i style={{ width: `${jobPercent(job)}%` }}/></div>
          <div className="job-progress__foot"><small>{Math.round(job.elapsed)}s elapsed · usually 1 to 2 minutes on the free Groq tier</small>
            <button type="button" className="text-button danger" onClick={cancelJob}>Cancel</button></div></div>}
        <div ref={threadEnd}/>
      </div>

      <div className="ask__composer">
        {error && <p className="memory-error" role="alert">{error}</p>}
        <div className="composer-scope" aria-label="Current scope">
          <span className="composer-scope__label">Searching</span>
          {scope.length === 0 && <span className="scope-chip scope-chip--all">Whole repository</span>}
          {scope.slice(0, 4).map(path => <span className="scope-chip" key={path} title={path}>
            <span>{path.split("/").pop()}</span>
            <button type="button" aria-label={`Remove ${path} from the scope`} onClick={() => setScope(rows => rows.filter(row => row !== path))}>×</button></span>)}
          {scope.length > 4 && <button type="button" className="scope-chip scope-chip--more" onClick={() => setDrawer(true)}>+{scope.length - 4} more</button>}
          {offerOpenFile && <button type="button" className="scope-chip scope-chip--add" title={`Add ${selectedPath} to the scope`}
            onClick={() => setScope(rows => normalize([...rows, selectedPath!]))}>+ {openFileName}</button>}
        </div>
        <form className="composer" onSubmit={ask}>
          <textarea ref={input} className="composer__input" aria-label="Ask engineering memory" rows={2}
            value={question} onChange={event => setQuestion(event.target.value.slice(0, MAX_QUESTION))} onKeyDown={onKeyDown}
            placeholder="Ask what something does, where it is, or who changed it…" required minLength={2} maxLength={MAX_QUESTION} />
          <div className="composer__row">
            <DepthControl value={depth} onChange={chooseDepth} disabled={busy} />
            <span className="composer__count" aria-hidden="true">{question.length}/{MAX_QUESTION}</span>
            <button className="button button-primary composer__send" disabled={busy || !branch || question.trim().length < 2}>
              {busy ? "Working…" : "Ask"}</button>
          </div>
        </form>
        <p className="composer__hint"><strong>{depthInfo.label}.</strong> {depthInfo.detail}</p>
        <small className="composer__foot">{aiLine}{aiLine ? " · " : ""}Enter to send, Shift+Enter for a new line. Sync after new pushes to keep answers current.</small>
      </div>
    </div>

    <div className="ask__splitter ask__splitter--knowledge" aria-label="Resize the knowledge panel" {...knowledge.handle} />
    <aside className="ask__knowledge" aria-label="Retrieved knowledge">
      <div className="knowledge__head"><p className="eyebrow">RETRIEVED KNOWLEDGE</p><h3>Code, history and sources</h3>
        <button type="button" className="ask__back" onClick={() => setPane("chat")}>← Conversation</button></div>
      <KnowledgePane answer={current} index={Math.max(index, 0)} flash={flash} onOpenFile={onOpenFile}
        onCite={label => cite(label, Math.max(index, 0))} side={sideContent}
        onPickFolder={path => setScope(rows => covered(path, rows) ? rows : normalize([...rows, path]))} />
    </aside>
    {drawer && <button type="button" className="ask__backdrop" aria-label="Close the scope panel" onClick={() => setDrawer(false)} />}
  </section>;
}
