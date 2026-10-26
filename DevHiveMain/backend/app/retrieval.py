"""Scope decides where knowledge comes from; depth decides how much of it is read.

Every folder in the selected scope (the whole repository when nothing is selected) is covered at
every depth: each one gets a digest line, and folders that match the question also get code
excerpts. The depth level only changes the quality: how many sections are picked per folder, how
large the window around each section is, how much of every window is sent and how detailed the
answer is.

Chunks are small (about 25 lines) so they embed and rank precisely. The picked chunks become
*seeds*: each is grown into a window by merging its neighbours in the same file, so the model reads
real context instead of a 25-line fragment.
"""

import re
from collections import Counter
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import Settings
from .models import FileChunk
from .rag import CODE_SUFFIXES, _vector_id, branch_file_chunks, hybrid_rank, rank_linked_files
from .scope import group_key


CHARS_PER_TOKEN = 3.2
PROMPT_OVERHEAD_TOKENS = 600  # system prompt, question and message framing
TPM_SAFETY = 0.92
EVIDENCE_FLOOR = 1500
WINDOW_OVERSHOOT = 1.25  # a window may grow to this multiple of window_lines
DUPLICATE_SIMILARITY = 0.9
RELEVANCE_CUTOFF = 0.35  # pointed questions: folders below this share of the best folder get a digest only
# Characters a code window costs beyond its code: the header line, and history/contributors/imports
# (sent once per file). The smallest excerpt worth sending is MIN_EXCERPT characters of code.
HEADER_CHARS = 130
EXTRAS_CHARS = {"none": 0, "brief": 150, "full": 350}
MIN_EXCERPT = 150
MIN_SECTION = 300  # a folder's second and later sections are sent only if each gets about 10 lines

# Caps; resolve_profile lowers the character budgets to what one call can carry at the TPM limit.
DEPTH_PROFILES: dict[str, dict] = {
    "low": dict(per_folder=1, sections=3, per_file_in_folder=1, window_lines=25, evidence_chars=5000, digest_chars=1200,
                readme_chars=80, tail_chars=300, extras="none", completion_tokens=800, effort="low",
                history=0, commits=1, max_wait=20, map_reduce=False),
    "medium": dict(per_folder=2, sections=6, per_file_in_folder=1, window_lines=40, evidence_chars=10000, digest_chars=1500,
                   readme_chars=160, tail_chars=800, extras="brief", completion_tokens=1000, effort="low",
                   history=2, commits=3, max_wait=25, map_reduce=False),
    "high": dict(per_folder=3, sections=9, per_file_in_folder=2, window_lines=60, evidence_chars=30000, digest_chars=1800,
                 readme_chars=160, tail_chars=1200, extras="full", completion_tokens=1500, effort="low",
                 history=4, commits=4, max_wait=60, map_reduce=False),
    "very_high": dict(per_folder=5, sections=16, per_file_in_folder=2, window_lines=60, evidence_chars=26000, digest_chars=2500,
                      readme_chars=160, tail_chars=3000, extras="full", map_chars=6500, reduce_chars=9000,
                      completion_tokens=550, reduce_tokens=2100, effort="low", reduce_effort="low",
                      map_batches=4, history=3, commits=6, max_wait=75, map_reduce=True),
}
DEPTHS = tuple(DEPTH_PROFILES)

BROAD_QUESTION = re.compile(
    r"\b(overview|summar\w*|each|every|all|chapters?|modules?|folders?|structure|architecture|whole|entire)\b", re.I)
DOCS_QUESTION = re.compile(r"\b(docs?|documentation|readme|config|yaml|json|workflow|markdown)\b", re.I)


def resolve_profile(depth: str, tpm: int) -> dict:
    """The depth's profile with character and token budgets clamped so one call fits the TPM budget."""
    profile = dict(DEPTH_PROFILES[depth], depth=depth)
    usable = int(tpm * TPM_SAFETY)
    for key in ("completion_tokens", "reduce_tokens"):
        if key in profile:
            profile[key] = min(profile[key], usable // 3)

    def room(completion: int) -> int:
        return int((usable - completion - PROMPT_OVERHEAD_TOKENS) * CHARS_PER_TOKEN)

    if profile["map_reduce"]:
        profile["map_chars"] = max(EVIDENCE_FLOOR, min(profile["map_chars"], room(profile["completion_tokens"])))
        profile["reduce_chars"] = max(EVIDENCE_FLOOR, min(profile["reduce_chars"], room(profile["reduce_tokens"])))
        # All map calls together carry the evidence; the digest goes to the final call.
        profile["evidence_chars"] = min(profile["evidence_chars"], profile["map_chars"] * profile["map_batches"])
    else:
        profile["evidence_chars"] = max(EVIDENCE_FLOOR,
                                        min(profile["evidence_chars"], room(profile["completion_tokens"])))
    return profile


def is_broad(question: str, scope: list[str], depth: str) -> bool:
    """Overview-style questions (or several folders, or Very high) give every folder code excerpts."""
    return depth == "very_high" or len(scope) >= 2 or bool(BROAD_QUESTION.search(question))


def wants_documents(question: str, broad: bool) -> bool:
    return broad or bool(DOCS_QUESTION.search(question))


# --- Selection --------------------------------------------------------------

def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_readme(chunk: FileChunk) -> bool:
    return _basename(chunk.path).lower().startswith("readme")


def _token_set(chunk: FileChunk) -> frozenset:
    return frozenset(re.findall(r"\w+", chunk.body.split("\n", 1)[-1]))


def collapse_duplicates(ranked: list[FileChunk]) -> tuple[list[FileChunk], dict[int, list[str]]]:
    """Drop near-identical chunks of identically named files; map the kept chunk to the other paths.

    Repositories often hold the same file once per chapter or example. Keeping every copy would
    spend the evidence budget on one piece of code, and score ties would always pick the first folder.
    """
    kept: list[FileChunk] = []
    tokens: dict[int, frozenset] = {}
    duplicates: dict[int, list[str]] = {}
    for chunk in ranked:
        match = None
        for other in kept:
            if other.path == chunk.path or other.symbol != chunk.symbol or _basename(other.path) != _basename(chunk.path):
                continue
            mine = tokens.setdefault(chunk.id, _token_set(chunk))
            theirs = tokens.setdefault(other.id, _token_set(other))
            union = len(mine | theirs)
            if union and len(mine & theirs) / union >= DUPLICATE_SIMILARITY:
                match = other
                break
        if match is None:
            kept.append(chunk)
        else:
            duplicates.setdefault(_vector_id(match), []).append(chunk.path)
    return kept, duplicates


def folder_relevance(chunks: list[FileChunk], scores: dict[int, float], scope: list[str]) -> dict[str, float]:
    """Best ranking score per folder (0 for folders with no matching chunk)."""
    relevance: dict[str, float] = {}
    for chunk in chunks:
        name = group_key(chunk.path, scope)
        relevance[name] = max(relevance.get(name, 0.0), scores.get(_vector_id(chunk), 0.0))
    return relevance


def select_per_folder(ranked: list[FileChunk], allowed: list[FileChunk], scores: dict[int, float],
                      scope: list[str], profile: dict, broad: bool) -> tuple[list[FileChunk], dict[str, float], set[str]]:
    """Seeds for every folder that should get code: its best chunks.

    Each folder gets at least `per_folder` sections; when few folders take part (a pointed question
    or a small scope) the depth's `sections` total is spread over them instead, so a narrow
    question still reads several places.

    Returns (seeds, relevance per folder, folders that get excerpts). Overview questions give every
    folder excerpts, a folder with no matching chunk being represented by its README, else its first
    chunk. Pointed questions only give excerpts to folders within RELEVANCE_CUTOFF of the best one,
    and none when no code matched (a history question); the digest lines still cover every folder.
    """
    relevance = folder_relevance(allowed, scores, scope)
    top = max(relevance.values(), default=0.0)
    wanted = {name for name, value in relevance.items()
              if broad or (top > 0.0 and value >= RELEVANCE_CUTOFF * top)}
    by_group: dict[str, list[FileChunk]] = {}
    for chunk in allowed:
        by_group.setdefault(group_key(chunk.path, scope), []).append(chunk)
    # A folder holding a single file may take all its sections from that file.
    single_file = {name for name, rows in by_group.items() if len({row.path for row in rows}) == 1}
    limit = max(profile["per_folder"], -(-profile.get("sections", 0) // max(1, len(wanted))))
    picked: dict[str, list[FileChunk]] = {}
    per_file: Counter = Counter()
    for chunk in ranked:  # best first
        name = group_key(chunk.path, scope)
        if name not in wanted or len(picked.get(name, [])) >= limit:
            continue
        if per_file[chunk.path] >= profile["per_file_in_folder"] and name not in single_file:
            continue
        picked.setdefault(name, []).append(chunk)
        per_file[chunk.path] += 1
    for name in wanted:
        # Top up folders that ranking left short (an overview question shares few words with most
        # code): the README first, then the opening section of further files, up to `per_folder`.
        rows = by_group[name]
        have = picked.setdefault(name, [])
        openers = sorted((row for row in rows if row.segment == 0), key=lambda row: (not _is_readme(row), row.path))
        for row in openers:
            if len(have) >= limit:
                break
            if all(row.path != seed.path for seed in have):
                have.append(row)
        if not have:
            have.append(rows[0])
    seeds = [chunk for name in sorted(picked) for chunk in picked[name]]
    return seeds, relevance, wanted


def chunks_by_path(chunks: list[FileChunk]) -> dict[str, list[FileChunk]]:
    grouped: dict[str, list[FileChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.path, []).append(chunk)
    for rows in grouped.values():
        rows.sort(key=lambda row: (row.start_line, row.segment))
    return grouped


def expand_windows(seeds: list[FileChunk], by_path: dict[str, list[FileChunk]], scores: dict[int, float],
                   window_lines: int, scope: list[str],
                   duplicates: dict[int, list[str]] | None = None) -> list[dict]:
    """Grow each seed into a window of neighbouring chunks, then merge windows that touch.

    Text is rebuilt from line numbers, so the overlap between neighbouring chunks appears once.
    Returns windows best-first with path, line range, code, score and folder group.
    """
    duplicates = duplicates or {}
    spans: dict[str, list[list]] = {}  # path -> [lo, hi, seed chunks]
    rows_of: dict[str, list[FileChunk]] = {}
    for seed in seeds:
        rows = rows_of.setdefault(seed.path, by_path.get(seed.path) or [seed])
        index = next((i for i, row in enumerate(rows) if row.id == seed.id), 0)
        lo = hi = index
        limit = window_lines * WINDOW_OVERSHOOT
        while rows[hi].end_line - rows[lo].start_line + 1 < window_lines:
            before = rows[lo - 1] if lo > 0 else None
            after = rows[hi + 1] if hi + 1 < len(rows) else None
            options = [(scores.get(_vector_id(row), 0.0), side, row)
                       for side, row in ((1, after), (0, before)) if row is not None]
            if not options:
                break
            _, side, row = max(options, key=lambda option: (option[0], option[1]))
            span = (row.end_line if side else rows[hi].end_line) - (rows[lo].start_line if side else row.start_line) + 1
            if span > limit:
                break
            lo, hi = (lo, hi + 1) if side else (lo - 1, hi)
        spans.setdefault(seed.path, []).append([lo, hi, [seed]])
    windows = []
    for path, found in spans.items():
        rows = rows_of[path]
        found.sort(key=lambda span: span[0])
        merged: list[list] = []
        for lo, hi, members in found:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
                merged[-1][2].extend(members)
            else:
                merged.append([lo, hi, list(members)])
        for lo, hi, members in merged:
            parts = rows[lo:hi + 1]
            lines: dict[int, str] = {}
            for part in parts:
                for offset, text in enumerate(part.body.split("\n", 1)[-1].split("\n")):
                    lines.setdefault(part.start_line + offset, text)
            start, end = parts[0].start_line, parts[-1].end_line
            best = max(members, key=lambda seed: scores.get(_vector_id(seed), 0.0))
            symbols = list(dict.fromkeys(part.symbol for part in parts if part.symbol))[:6]
            windows.append({
                "path": path, "start_line": start, "end_line": end,
                "symbol": best.symbol or (symbols[0] if symbols else None), "symbols": symbols,
                "code": "\n".join(lines[n] for n in range(start, end + 1) if n in lines),
                "score": round(max(scores.get(_vector_id(seed), 0.0) for seed in members), 4),
                "seeds": len(members), "sections": len(parts), "head_sha": best.head_sha,
                "focus_start": best.start_line, "focus_end": best.end_line,
                "group": group_key(path, scope),
                "duplicates": sorted({other for seed in members for other in duplicates.get(_vector_id(seed), [])}),
            })
    windows.sort(key=lambda window: (-window["score"], window["path"], window["start_line"]))
    return windows


# --- Coverage and budget ----------------------------------------------------

def trim_code(code: str, start_line: int | None, focus_start: int | None, focus_end: int | None,
              limit: int) -> str:
    """Cut code to `limit` characters, keeping the lines around the best-matching section."""
    if len(code) <= limit:
        return code
    lines = code.split("\n")
    first = start_line or 1
    lo = min(max((focus_start or first) - first, 0), len(lines) - 1)
    hi = min(max((focus_end or focus_start or first) - first, lo), len(lines) - 1)
    size = sum(len(line) + 1 for line in lines[lo:hi + 1])
    while size < limit:  # grow outward from the focus, alternating after / before
        grew = False
        if hi + 1 < len(lines) and size + len(lines[hi + 1]) + 1 <= limit:
            hi += 1
            size += len(lines[hi]) + 1
            grew = True
        if lo > 0 and size + len(lines[lo - 1]) + 1 <= limit:
            lo -= 1
            size += len(lines[lo]) + 1
            grew = True
        if not grew:
            break
    return "\n".join(lines[lo:hi + 1])[:limit]


def folder_digests(chunks: list[FileChunk], scope: list[str], relevance: dict[str, float],
                   scores: dict[int, float], profile: dict) -> dict | None:
    """One line per folder in scope: file and section counts, key functions, the README's opening.

    This is what guarantees coverage: it is always sent, so every folder is named even when the
    budget leaves no room for its code. Lines shrink as the folder count grows.
    """
    if not chunks:
        return None
    folders: dict[str, dict] = {}
    readmes: dict[str, FileChunk] = {}
    for chunk in chunks:
        name = group_key(chunk.path, scope)
        entry = folders.setdefault(name, {"files": set(), "chunks": 0, "symbols": Counter(), "best": {}})
        entry["files"].add(chunk.path)
        entry["chunks"] += 1
        if chunk.symbol:
            entry["symbols"][chunk.symbol] += 1
            score = scores.get(_vector_id(chunk), 0.0)
            entry["best"][chunk.symbol] = max(entry["best"].get(chunk.symbol, 0.0), score)
        if _is_readme(chunk) and chunk.segment == 0:
            if name not in readmes or chunk.path.count("/") < readmes[name].path.count("/"):
                readmes[name] = chunk
    rows = []
    for name in sorted(folders, key=lambda key: [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", key)]):
        entry = folders[name]
        ranked = sorted(entry["symbols"], key=lambda s: (-entry["best"].get(s, 0.0), -entry["symbols"][s], s))
        readme = readmes.get(name)
        # Drop comment banners ("*****", "=====", "// ----") so the opening words are real prose.
        prose = re.sub(r"[*=#/~_\-]{3,}", " ", readme.body.split("\n", 1)[-1]) if readme else ""
        snippet = re.sub(r"\s+", " ", re.sub(r"(^|\s)[*#/]+(?=\s)", " ", prose)).strip()[:profile["readme_chars"]]
        rows.append({"path": name, "files": len(entry["files"]), "chunks": entry["chunks"], "readme": snippet,
                     "symbols": ranked[:5], "relevance": round(relevance.get(name, 0.0), 3), "excerpts": False})
    width = max(60, profile["digest_chars"] // max(1, len(rows)))
    lines = []
    for row in rows:
        line = f"- {row['path']}: {row['files']} files, {row['chunks']} sections"
        if row["symbols"]:
            line += "; key: " + ", ".join(row["symbols"])
        if row["readme"]:
            line += f". README: {row['readme']}"
        lines.append(line[:width])
    text = "Folders in scope (every one must be covered):\n" + "\n".join(lines)
    if len(text) > profile["evidence_chars"] * 0.6:  # very many folders: names and counts only
        text = "Folders in scope (every one must be covered): " + "; ".join(
            f"{row['path']} ({row['files']})" for row in rows)
    return {"folders": rows, "text": text}


def budget_windows(windows: list[dict], relevance: dict[str, float], profile: dict, digest_chars: int,
                   broad: bool = False) -> list[dict]:
    """Share the evidence budget between folders and set each window's `excerpt` (the code sent).

    Every folder that has windows gets at least the cost of one minimal excerpt. The rest follows
    relevance: strongly for a pointed question, only mildly for an overview, where every folder
    deserves about the same room. When even the minimum does not fit, the least relevant folders keep
    only their digest line. Returns the windows that made it, grouped by folder (most relevant first)
    and best-first within each folder.
    """
    extras = EXTRAS_CHARS[profile["extras"]]
    floor = HEADER_CHARS + extras + MIN_EXCERPT
    remaining = max(0, profile["evidence_chars"] - digest_chars - profile["tail_chars"])
    by_group: dict[str, list[dict]] = {}
    for window in windows:
        by_group.setdefault(window["group"], []).append(window)
    order = sorted(by_group, key=lambda name: (-relevance.get(name, 0.0), name))
    keep = order[:max(1, remaining // floor)] if order else []
    weights = {name: (1.0 + 0.5 * relevance.get(name, 0.0)) if broad else (relevance.get(name, 0.0) + 0.25)
               for name in keep}
    spare = max(0, remaining - floor * len(keep))
    total = sum(weights.values()) or 1.0
    kept: list[dict] = []
    seen_files: set[str] = set()
    for name in keep:
        share = floor + int(spare * weights[name] / total)
        ordered = sorted(by_group[name], key=lambda w: (-w["score"], w["path"], w["start_line"]))
        # Use as many sections as the share allows while each still carries a useful excerpt.
        count = len(ordered)
        while count > 1:
            files: set[str] = set(seen_files)
            costs = []
            for window in ordered[:count]:
                costs.append(HEADER_CHARS + (0 if window["path"] in files else extras))
                files.add(window["path"])
            if all(share // count - cost >= MIN_SECTION for cost in costs):
                break
            count -= 1
        part = share // count
        for window in ordered[:count]:
            cost = HEADER_CHARS + (0 if window["path"] in seen_files else extras)
            excerpt = trim_code(window["code"], window["start_line"], window["focus_start"], window["focus_end"],
                                max(MIN_EXCERPT, part - cost))
            window["excerpt"] = excerpt
            window["extras"] = window["path"] not in seen_files
            seen_files.add(window["path"])
            kept.append(window)
    return kept


def linked_windows(db: Session, settings: Settings, repo_id: str, branch: str, neighbors: list[str],
                   question: str, window_lines: int, limit: int = 2) -> list[dict]:
    """Windows from the best-matching files an open file includes or is used by (one per file)."""
    ranked = rank_linked_files(db, settings, repo_id, branch, neighbors, question, limit=8)
    seeds: list[FileChunk] = []
    for chunk in ranked:
        if chunk.path not in {seed.path for seed in seeds} and len(seeds) < limit:
            seeds.append(chunk)
    if not seeds:
        return []
    rows = db.execute(select(FileChunk).where(FileChunk.repository_id == repo_id, FileChunk.branch == branch,
        FileChunk.path.in_([seed.path for seed in seeds]))).scalars().all()
    windows = expand_windows(seeds, chunks_by_path(list(rows)), {}, window_lines, [])
    for window in windows:
        window["group"] = "linked files"
        window["excerpt"] = trim_code(window["code"], window["start_line"], window["focus_start"],
                                      window["focus_end"], 1500)
        window["extras"] = True
    return windows


def retrieve_evidence(db: Session, settings: Settings, repo_id: str, branch: str, question: str,
                      profile: dict, scope: list[str], query_vec: list[float],
                      broad: bool | None = None) -> dict:
    """Rank everything in scope, pick per folder, expand, then budget: {windows, outline, stats, pool}."""
    if broad is None:
        broad = is_broad(question, scope, profile["depth"])
    allowed = branch_file_chunks(db, settings, repo_id, branch, scope)
    stats = {"candidates": len(allowed), "ranked": 0, "windows": 0, "folders_in_scope": 0, "folders_covered": 0,
             "folders_with_code": 0, "duplicates_collapsed": 0, "avg_excerpt_chars": 0}
    if not allowed:
        return {"windows": [], "outline": None, "stats": stats, "pool": []}
    pool = allowed
    if not wants_documents(question, broad):  # ranking prefers code; coverage still reads everything
        code = [chunk for chunk in allowed if chunk.path.lower().endswith(CODE_SUFFIXES)]
        pool = code or allowed
    scores: dict[int, float] = {}
    ranked = hybrid_rank(db, settings, repo_id, pool, query_vec, question, len(pool), scores=scores)
    ranked, duplicates = collapse_duplicates(ranked)
    stats["ranked"] = len(ranked)
    stats["duplicates_collapsed"] = sum(len(paths) for paths in duplicates.values())
    seeds, relevance, _ = select_per_folder(ranked, allowed, scores, scope, profile, broad)
    windows = expand_windows(seeds, chunks_by_path(allowed), scores, profile["window_lines"], scope, duplicates)
    outline = folder_digests(allowed, scope, relevance, scores, profile)
    windows = budget_windows(windows, relevance, profile, len(outline["text"]), broad)
    with_code: dict[str, int] = {}
    for window in windows:
        with_code[window["group"]] = with_code.get(window["group"], 0) + len(window["excerpt"])
    for row in outline["folders"]:
        row["excerpts"] = row["path"] in with_code
    stats.update(windows=len(windows), folders_in_scope=len(outline["folders"]),
                 folders_covered=len(outline["folders"]), folders_with_code=len(with_code),
                 avg_excerpt_chars=round(sum(with_code.values()) / len(with_code)) if with_code else 0)
    return {"windows": windows, "outline": outline, "stats": stats, "pool": allowed}
