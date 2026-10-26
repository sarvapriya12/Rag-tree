"""Canonical memory in SQL; hybrid BM25 + dense retrieval with optional Supabase/TurboVec."""

import hashlib
import json
import math
import re
from collections import Counter
from threading import Lock
from functools import lru_cache
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from .config import Settings
from .models import BranchEmbeddingState, BranchSnapshot, CommitRecord, FileChunk, MemoryChunk
from .permissions import Permission, effective_bits
from .scope import covers, scope_clause

try:  # numpy ships with the retrieval extras; plain Python is the fallback.
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


DIM = 384
MAX_EMBED_TOKENS = 256  # MiniLM's native limit; a 25-line chunk is about 250 tokens
RRF_K = 60
LARGE_CORPUS = 3000
_TURBO_LOCK = Lock()
_TURBO_CACHE: dict[str, tuple[tuple, object]] = {}
_FEATURE_LOCK = Lock()
_FEATURE_CACHE: dict[str, tuple[tuple, dict]] = {}
CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs", ".rb",
                 ".php", ".sh", ".sql", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
                 ".m", ".mm", ".swift")
HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx")
STOPWORDS = frozenset("""a an and are as at be but by can could did do does for from had has have how i if in
into is it its me my of on or our please should so than that the their them then there these they this to
us was we were what when where which who whom why will with would you your tell show explain about give
find look code file files function functions implemented implementation
before after used use using way make makes made""".split())


def embedding_profile(settings: Settings) -> str:
    return f"semantic-v5:{settings.embedding_model}" if settings.embedding_model else "lexical-v5"


def invalidate_turbovec(repository_id: str) -> None:
    with _TURBO_LOCK:
        _TURBO_CACHE.pop(repository_id, None)
    with _FEATURE_LOCK:
        _FEATURE_CACHE.pop(repository_id, None)


_MODEL_LOCK = Lock()


def _model(name: str):
    # lru_cache alone lets the startup warm-up and the first question both load the model.
    with _MODEL_LOCK:
        return _load_model(name)


@lru_cache(maxsize=2)
def _load_model(name: str):
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(name)
    # Chunks are about 25 lines, so 256 tokens lets the dense vector see nearly all of the code.
    model.max_seq_length = min(getattr(model, "max_seq_length", None) or MAX_EMBED_TOKENS, MAX_EMBED_TOKENS)
    return model


def embed_many(bodies: list[str], settings: Settings) -> list[list[float]]:
    if not bodies:
        return []
    if settings.embedding_model:
        rows = _model(settings.embedding_model).encode(bodies, normalize_embeddings=True)
        if any(len(row) != DIM for row in rows):
            raise ValueError(f"Embedding model must return {DIM} dimensions")
        return [[float(x) for x in row] for row in rows]
    # Stable local fallback for development: lexical feature hashing, not semantic embeddings.
    result = []
    for body in bodies:
        vec = [0.0] * DIM
        tokens = re.findall(r"[\w]{2,}", body.lower())
        for token in tokens:
            features = [token] + [token[i:i + 3] for i in range(max(0, len(token) - 2))]
            for feature in features:
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % DIM
                vec[index] += 1.0 if digest[4] & 1 else -1.0
        magnitude = math.sqrt(sum(v * v for v in vec)) or 1.0
        result.append([v / magnitude for v in vec])
    return result


def embed(body: str, settings: Settings) -> list[float]:
    return embed_many([body], settings)[0]


def store_chunk(db: Session, settings: Settings, *, repo_id: str, branch: str,
                commit_id: str, kind: str, body: str,
                vector: list[float] | None = None) -> MemoryChunk:
    chunk = MemoryChunk(repository_id=repo_id, branch=branch, commit_id=commit_id,
                        kind=kind, body=body,
                        embedding_json=json.dumps(vector if vector is not None else embed(body, settings)))
    db.add(chunk)
    db.flush()
    if settings.rag_backend in {"supabase", "turbovec"} and db.bind.dialect.name == "postgresql":
        db.execute(text("""INSERT INTO memory_vectors(chunk_id,embedding)
                       VALUES (:id, CAST(:embedding AS extensions.vector))"""),
                   {"id": chunk.id, "embedding": chunk.embedding_json})
    invalidate_turbovec(repo_id)
    return chunk


# --- Tokens and BM25 -------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _stem(token: str) -> str:
    """Tiny suffix stripper so trained/training/train and plate/plates meet."""
    if len(token) < 5 or token.isdigit():
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 6:
        return token[:-3]
    if token.endswith("ed") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(body: str) -> list[str]:
    """Identifiers contribute the whole name and its stemmed camelCase/snake_case parts.

    Parts of six or more letters also emit a "~" prefix token, so detect, detector and
    detection (or recognition and recognize) share a term.
    """
    tokens: list[str] = []
    for ident in _IDENT.findall(body):
        parts = [p.lower() for p in _CAMEL.findall(ident) if len(p) > 1 or p.isdigit()]
        whole = ident.replace("_", "").lower()
        if len(whole) > 1 and (len(parts) > 1 or "_" in ident):
            tokens.append(whole)
        for part in parts:
            stem = _stem(part)
            tokens.append(stem)
            if len(stem) >= 6 and not stem.isdigit():
                tokens.append("~" + stem[:6])
        if len(parts) == 1 and parts[0] != whole and len(whole) > 1:
            tokens.append(whole)
    return tokens


_STOP_FORMS = STOPWORDS | {_stem(word) for word in STOPWORDS}
# "~" prefix tokens are the first six letters of a stem; a stopword's prefix must not leak through.
_STOP_PREFIXES = {word[:6] for word in _STOP_FORMS if len(word) >= 6}


def query_terms(question: str) -> list[str]:
    terms = []
    for ident in _IDENT.findall(question):
        low = ident.lower()
        if low in _STOP_FORMS or _stem(low) in _STOP_FORMS or ident.isdigit():
            continue
        terms.extend(t for t in tokenize(ident)
                     if t not in _STOP_FORMS and t.lstrip("~") not in _STOP_FORMS and not t.isdigit()
                     and not (t.startswith("~") and t[1:] in _STOP_PREFIXES))
    return list(dict.fromkeys(terms))


def _query_identifiers(question: str) -> set[str]:
    """Names that look like code identifiers; an exact symbol match deserves a boost."""
    found = set()
    for ident in _IDENT.findall(question):
        if len(ident) >= 4 and (("_" in ident) or re.search(r"[a-z][A-Z]", ident)
                                or (ident.isalpha() and ident.lower() not in STOPWORDS)):
            found.add(ident.replace("_", "").lower())
    return found


_NON_CODE = ("//", "/*", "*", "#", "--", "\"\"\"", "'''")


def _code_ratio(chunk: MemoryChunk | FileChunk) -> float:
    """Share of non-blank lines that are code rather than comments, includes or imports."""
    if not isinstance(chunk, FileChunk):
        return 1.0
    lines = [line.strip() for line in chunk.body.split("\n")[1:]]
    lines = [line for line in lines if line]
    if not lines:
        return 0.0
    code = sum(1 for line in lines if not line.startswith(_NON_CODE))
    return code / len(lines)


class _Feature:
    __slots__ = ("counts", "length", "vector", "code_ratio", "name_terms")

    def __init__(self, chunk: MemoryChunk | FileChunk):
        tokens = tokenize(chunk.body)
        self.counts = Counter(tokens)
        self.length = len(tokens) or 1
        self.code_ratio = _code_ratio(chunk)
        self.name_terms: frozenset = frozenset()
        if isinstance(chunk, FileChunk):
            # Field boost: a query word that appears in the function or file name counts triple.
            name = (chunk.path or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            named = tokenize(f"{chunk.symbol or ''} {name}")
            self.name_terms = frozenset(t for t in named if not t.startswith("~"))
            for token in named:
                self.counts[token] += 2
        values = json.loads(chunk.embedding_json)
        self.vector = np.asarray(values, dtype=np.float32) if np is not None else values


def _repo_version(db: Session, repo_id: str) -> tuple:
    count, maximum = db.execute(select(func.count(MemoryChunk.id), func.max(MemoryChunk.id))
                                .where(MemoryChunk.repository_id == repo_id)).one()
    file_count, file_maximum = db.execute(select(func.count(FileChunk.id), func.max(FileChunk.id))
                                          .where(FileChunk.repository_id == repo_id)).one()
    profile_stamp = db.execute(select(func.max(BranchEmbeddingState.updated_at)).where(
        BranchEmbeddingState.repository_id == repo_id)).scalar()
    snapshot_stamp = db.execute(select(func.max(BranchSnapshot.indexed_at)).where(
        BranchSnapshot.repository_id == repo_id)).scalar()
    return (count, maximum or 0, file_count, file_maximum or 0, profile_stamp, snapshot_stamp)


def _vector_id(chunk: MemoryChunk | FileChunk) -> int:
    return chunk.id * 2 + int(isinstance(chunk, FileChunk))


def _features(db: Session, repo_id: str, chunks: list[MemoryChunk | FileChunk]) -> list[_Feature]:
    """Decode embeddings and tokens once per repository version, not once per query."""
    version = _repo_version(db, repo_id)
    with _FEATURE_LOCK:
        cached = _FEATURE_CACHE.get(repo_id)
        if cached is None or cached[0] != version:
            if len(_FEATURE_CACHE) >= 16:
                _FEATURE_CACHE.pop(next(iter(_FEATURE_CACHE)))
            cached = (version, {})
            _FEATURE_CACHE[repo_id] = cached
        store = cached[1]
        result = []
        for chunk in chunks:
            key = _vector_id(chunk)
            feature = store.get(key)
            if feature is None:
                feature = store[key] = _Feature(chunk)
            result.append(feature)
    return result


def bm25_scores(features: list[_Feature], terms: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """Okapi BM25 over the candidate set only, so scores never reflect unreadable branches."""
    n = len(features)
    if not n or not terms:
        return [0.0] * n
    average = sum(f.length for f in features) / n
    scores = [0.0] * n
    for term in terms:
        df = sum(1 for f in features if term in f.counts)
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, feature in enumerate(features):
            tf = feature.counts.get(term, 0)
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * feature.length / average))
    return scores


def _idf(features: list[_Feature], terms: list[str]) -> dict[str, float]:
    n = len(features)
    result = {}
    for term in terms:
        df = sum(1 for f in features if term in f.counts)
        if df:
            result[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    return result


def _cosines(features: list[_Feature], query_vec: list[float]) -> list[float]:
    if np is not None:
        if not features:
            return []
        matrix = np.vstack([f.vector for f in features])
        return (matrix @ np.asarray(query_vec, dtype=np.float32)).tolist()
    return [sum(x * y for x, y in zip(f.vector, query_vec)) for f in features]


def hybrid_rank(db: Session, settings: Settings, repo_id: str,
                chunks: list[MemoryChunk | FileChunk], query_vec: list[float], question: str,
                limit: int, per_file: int | None = None,
                scores: dict[int, float] | None = None) -> list[MemoryChunk | FileChunk]:
    """Reciprocal-rank fusion of dense similarity and BM25 over already-authorized chunks.

    When `scores` is a dict it receives {vector id: fused score / best fused score} for every
    candidate that passed the relevance floor, so callers can show or reuse the ranking.
    """
    if not chunks:
        return []
    semantic = bool(settings.embedding_model)
    terms = query_terms(question)
    features = _features(db, repo_id, chunks)
    keyword = bm25_scores(features, terms)
    similarity = _cosines(features, query_vec)
    if len(chunks) > LARGE_CORPUS and settings.rag_backend == "turbovec":
        # Exact cosine over a huge corpus is slow; let the accelerator choose dense candidates.
        picked = {_vector_id(c) for c in _rank_turbovec(db, repo_id, chunks, query_vec, 300)}
        similarity = [s if _vector_id(c) in picked else -1.0 for s, c in zip(similarity, chunks)]
    dense_order = sorted((i for i in range(len(chunks)) if similarity[i] > 0.0),
                         key=lambda i: similarity[i], reverse=True)
    keyword_order = sorted((i for i in range(len(chunks)) if keyword[i] > 0.0),
                           key=lambda i: keyword[i], reverse=True)
    fused: dict[int, float] = {}
    for rank, i in enumerate(dense_order[:200]):
        if semantic or keyword[i] > 0:
            fused[i] = fused.get(i, 0.0) + (1.0 if semantic else 0.6) / (RRF_K + rank + 1)
    for rank, i in enumerate(keyword_order[:200]):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    identifiers = _query_identifiers(question)
    idf = _idf(features, [t for t in terms if not t.startswith("~")])
    wants_header = bool(re.search(r"\b(header|declar\w*|interface|prototype|signature|api)\b", question, re.I))
    for i in list(fused):
        chunk = chunks[i]
        symbol = (getattr(chunk, "symbol", None) or "").replace("_", "").lower()
        path = (getattr(chunk, "path", None) or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("_", "").lower()
        if identifiers and (symbol in identifiers or (path and path in identifiers)):
            fused[i] += 0.03
        if features[i].name_terms:
            # Rare query words found in the function or file name are strong evidence.
            fused[i] += 0.006 * sum(idf.get(t, 0.0) for t in features[i].name_terms if t in idf)
        if isinstance(chunk, FileChunk):
            # Comment/include-only chunks and bare declarations rarely answer "how"/"where".
            fused[i] *= 0.55 + 0.45 * features[i].code_ratio
            if chunk.path.lower().endswith(HEADER_SUFFIXES) and not wants_header:
                fused[i] *= 0.85
    minimum = 0.2 if semantic else 0.0
    ordered = [i for i in sorted(fused, key=fused.get, reverse=True)
               if keyword[i] > 0 or similarity[i] >= minimum]
    if scores is not None and ordered:
        top = fused[ordered[0]] or 1.0
        scores.update({_vector_id(chunks[i]): fused[i] / top for i in ordered})
    result: list[MemoryChunk | FileChunk] = []
    seen: Counter = Counter()
    for i in ordered:
        chunk = chunks[i]
        if per_file is not None and isinstance(chunk, FileChunk):
            if seen[chunk.path] >= per_file:
                continue
            seen[chunk.path] += 1
        result.append(chunk)
        if len(result) >= limit:
            break
    return result


def commits_touching(db: Session, repo_id: str, branches: list[str], scope: list[str]) -> set[str]:
    """Ids of imported commits whose changed files fall inside the scope (newest 500 per branch)."""
    rows = db.execute(select(CommitRecord).where(CommitRecord.repository_id == repo_id,
        CommitRecord.branch.in_(branches)).order_by(CommitRecord.committed_at.desc()).limit(500 * len(branches))).scalars()
    found = set()
    for commit in rows:
        try:
            files = json.loads(commit.files_json)
        except ValueError:
            continue
        if any(covers(str(item.get("name", "")), scope) for item in files if isinstance(item, dict)):
            found.add(commit.id)
    return found


def _rank_turbovec(db: Session, repo_id: str, allowed: list[MemoryChunk | FileChunk],
                   query_vec: list[float], limit: int):
    from turbovec import IdMapIndex
    if not allowed:
        return []
    version = _repo_version(db, repo_id)
    with _TURBO_LOCK:
        cached = _TURBO_CACHE.get(repo_id)
        if cached is None or cached[0] != version:
            all_chunks = db.execute(select(MemoryChunk).where(MemoryChunk.repository_id == repo_id)).scalars().all()
            all_chunks += db.execute(select(FileChunk).where(FileChunk.repository_id == repo_id)).scalars().all()
            index = IdMapIndex(dim=DIM, bit_width=4)
            vectors = np.array([json.loads(c.embedding_json) for c in all_chunks], dtype=np.float32)
            ids = np.array([_vector_id(c) for c in all_chunks], dtype=np.uint64)
            index.add_with_ids(vectors, ids)
            if len(_TURBO_CACHE) >= 16:
                _TURBO_CACHE.pop(next(iter(_TURBO_CACHE)))
            _TURBO_CACHE[repo_id] = (version, index)
        else:
            index = cached[1]
        allowlist = np.array([_vector_id(c) for c in allowed], dtype=np.uint64)
        _, found_ids = index.search(np.array([query_vec], dtype=np.float32),
                                    k=min(limit, len(allowed)), allowlist=allowlist)
    by_id = {_vector_id(c): c for c in allowed}
    return [by_id[int(i)] for i in found_ids[0] if int(i) in by_id]


def search(db: Session, settings: Settings, repository_id: str, project, user,
           question: str, limit: int = 5, branch: str | None = None,
           query_vec: list[float] | None = None, scope: list[str] | None = None) -> dict:
    # The caller has already passed repository visibility. Filter each branch at query time,
    # so a revoked permission takes effect without reindexing.
    all_branches = set(db.execute(select(MemoryChunk.branch).where(
        MemoryChunk.repository_id == repository_id).distinct()).scalars().all())
    all_branches.update(db.execute(select(FileChunk.branch).where(
        FileChunk.repository_id == repository_id).distinct()).scalars().all())
    profile = embedding_profile(settings)
    ready = set(db.execute(select(BranchEmbeddingState.branch).where(
        BranchEmbeddingState.repository_id == repository_id,
        BranchEmbeddingState.profile == profile)).scalars().all())
    required = int(Permission.VIEW_MEMORY | Permission.VIEW_REPOSITORY)
    branches = [name for name in all_branches if name in ready and (branch is None or name == branch)
                and effective_bits(db, project, user, name) & required == required]
    if not branches:
        return {"answer": "No indexed memory for this branch. Choose Sync memory in the repository workspace first.", "citations": []}
    allowed = db.execute(select(MemoryChunk).where(
        MemoryChunk.repository_id == repository_id, MemoryChunk.branch.in_(branches))
        .order_by(MemoryChunk.id)).scalars().all()
    file_query = select(FileChunk).where(FileChunk.repository_id == repository_id,
                                         FileChunk.branch.in_(branches))
    if scope:
        # A scoped question only sees history that touched files inside the scope.
        touching = commits_touching(db, repository_id, branches, scope)
        allowed = [c for c in allowed if c.commit_id in touching]
        file_query = file_query.where(scope_clause(scope))
    allowed += db.execute(file_query.order_by(FileChunk.path, FileChunk.segment)).scalars().all()
    if query_vec is None:
        query_vec = embed(question, settings)
    # Keep commit history represented even when code chunks outnumber it: rank the pool,
    # then guarantee a few commit/TCC hits when they matched at all.
    ranked = hybrid_rank(db, settings, repository_id, allowed, query_vec, question,
                         limit * 3, per_file=2)
    history = [c for c in ranked if isinstance(c, MemoryChunk)]
    code = [c for c in ranked if isinstance(c, FileChunk)]
    reserve = min(len(history), max(2, limit // 2))
    picked = history[:reserve] + code[:max(limit - reserve, 0)]
    picked += [c for c in ranked if c not in picked][:max(limit - len(picked), 0)]
    ranked = sorted(picked, key=ranked.index)[:limit]
    citations = []
    for chunk in ranked:
        if isinstance(chunk, FileChunk):
            citations.append({"id": _vector_id(chunk), "sha": chunk.head_sha, "branch": chunk.branch,
                              "kind": "file", "message": chunk.path, "path": chunk.path,
                              "symbol": chunk.symbol, "start_line": chunk.start_line,
                              "end_line": chunk.end_line, "excerpt": chunk.body[:650]})
        else:
            commit = db.get(CommitRecord, chunk.commit_id)
            citations.append({"id": _vector_id(chunk), "sha": commit.sha, "branch": chunk.branch,
                              "kind": chunk.kind, "message": commit.message, "path": None,
                              "author": commit.author, "committed_at": commit.committed_at,
                              "excerpt": chunk.body[:650]})
    answer = "\n".join(f"[{c['branch']} @ {c['sha'][:12]} | {c['message']}] {c['excerpt'][:300]}" for c in citations[:3])
    return {"answer": answer or "No indexed source matched this question on this branch.", "citations": citations}


def branch_file_chunks(db: Session, settings: Settings, repository_id: str, branch: str,
                       scope: list[str] | None = None) -> list[FileChunk]:
    """Every indexed chunk of one authorized branch inside the scope, in path order.

    Callers enforce permissions. The order is fixed (path, segment) so score ties never favour
    whichever folder happens to hold the lowest ids.
    """
    state = db.get(BranchEmbeddingState, (repository_id, branch))
    if state is None or state.profile != embedding_profile(settings):
        return []
    query = select(FileChunk).where(FileChunk.repository_id == repository_id, FileChunk.branch == branch)
    if scope:
        query = query.where(scope_clause(scope))
    return list(db.execute(query.order_by(FileChunk.path, FileChunk.segment)).scalars().all())


def rank_linked_files(db: Session, settings: Settings, repository_id: str,
                      branch: str, paths: list[str], question: str, limit: int = 4) -> list[FileChunk]:
    """Rank only branch-local, graph-linked files; never search the whole repo here."""
    state = db.get(BranchEmbeddingState, (repository_id, branch))
    if not paths or state is None or state.profile != embedding_profile(settings):
        return []
    allowed = db.execute(select(FileChunk).where(FileChunk.repository_id == repository_id,
        FileChunk.branch == branch, FileChunk.path.in_(paths))
        .order_by(FileChunk.path, FileChunk.segment)).scalars().all()
    if not allowed:
        return []
    query_vec = embed(question, settings)
    return hybrid_rank(db, settings, repository_id, allowed, query_vec, question, limit)
