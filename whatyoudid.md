# What I did

I read `DevHive_Project_Description_and_30_Day_Timeline.docx` and reorganized its MVP into three dependency-based phases without deadlines. The plan is in [PHASES.md](PHASES.md) and in [DevHive_Three_Phase_Plan.docx](DevHive_Three_Phase_Plan.docx), a copy of the original Word document with the plan appended. The supplied Word file could be read but could not be saved in place (`PermissionError`), so I left it intact.

## Phase status

| Phase | Status | Delivered or planned |
| --- | --- | --- |
| 1. GitHub access, roles, and memory | Implemented locally | API, GitHub repository verification and commit import, users and projects, branch policies, TCC records, searchable memory, audit events, tests, documentation. |
| 2. Controlled Git workflow | Planned | GitHub OAuth, terminal TCC capture, server-side protected branch enforcement, verified push/webhook state machine, retryable queue. |
| 3. Semantic RAG and control plane | Planned | PostgreSQL/Qdrant migration, embeddings/LLM answers, dashboard, workers, deployment and security hardening. |

## What works in Phase 1

- An administrator can bootstrap the database once with `DEVHIVE_BOOTSTRAP_TOKEN`, create users, and hand each user a personal bearer token. Stored tokens are SHA-256 hashes, not plaintext.
- An authenticated user can register a GitHub repository. The API verifies its identity through GitHub. A DevHive administrator is required to register a private repository. The project creator becomes its owner.
- The project owner can assign role levels: observer (1), contributor (2), developer (3), maintainer (4), owner (5). The owner can edit branch patterns and minimum levels for read, push, and merge. Default rules allow `feature/*` pushes from level 2, `development` pushes from level 3, and `main` pushes and merges from level 4. A matching rule with greater specificity wins. Unspecified operations are denied.
- Members can list GitHub branches they are allowed to read and preview a small UTF-8 file from an authorized branch. A maintainer can import recent GitHub commits for a readable branch. Commit author, date, parent SHA, message, changed files, and limited diff context are stored. Repeated syncs skip existing commits.
- A member with push permission can attach one immutable Thought/Code/Comment (TCC) record to an imported commit on that branch. The TCC becomes a separate memory document.
- Search ranks commit and TCC documents with SQLite FTS5/BM25. It limits records to the requester's project membership and currently readable branches **inside the retrieval query**, then returns excerpts and commit citations. The response also assembles an extractive answer from the top hits. No external AI key is required.
- Access decisions, project changes, syncs, TCC writes, and searches are recorded in the audit table. Owners can inspect the project audit log.

## Files, folders, and functions

| Location | Responsibility |
| --- | --- |
| `devhive/api.py` | FastAPI routes and request validation. `create_app` assembles the API. `setup`, `add_user`, `add_project`, `set_member`, and `set_policy` manage identity and configuration. `access` evaluates a requested branch operation. `sync` imports GitHub commits. `branches` and `repository_file` expose authorized repository browsing. `commits`, `submit_tcc`, `search_memory`, and `audit_log` expose history and memory. Dependencies `get_db` and `get_user` create database connections and authenticate requests. |
| `devhive/auth.py` | `token_hash` hashes bearer tokens; `create_user` issues a high-entropy token; `authenticate` resolves a bearer token to its user. |
| `devhive/config.py` | `Settings.from_env` reads the SQLite path, bootstrap secret, and optional GitHub token. |
| `devhive/db.py` | SQLite schema and connection setup. `initialize` creates all tables, FTS index, and indexes. `connect` enables foreign keys. `audit` writes audit events. |
| `devhive/policy.py` | `membership` gets a user's project role; `matching_policy` chooses the most specific matching branch rule; `can` checks an operation; `require` records the decision and rejects denied operations. |
| `devhive/github.py` | `validate_slug` restricts repository identifiers; `GitHubClient.repository`, `commits`, `commit`, `branches`, and `file` call GitHub's REST API with bounded requests and optional token. File preview accepts only small UTF-8 content. |
| `devhive/memory.py` | `ingest_commit` stores immutable commit metadata; `add_document` indexes commit/TCC text; `search` performs permission-filtered BM25 retrieval; `answer` builds a citation-linked extractive response. |
| `devhive/__main__.py` | Starts the API at `127.0.0.1:8000` with `python -m devhive`. |
| `tests/test_api.py` | Standard-library `unittest` API tests with a fake GitHub client. Covers project/branch isolation, role checks, sync idempotency, TCC immutability, and search citations. |
| `scripts/build_plan_doc.py` | Rebuilds the revised Word copy from the original document and `PHASES.md`. |
| `README.md` | Local setup, API first-use sequence, access defaults, search behavior, and test command. |
| `PHASES.md` | Detailed three-phase plan and exit criteria. |
| `requirements.txt` | Runtime dependencies plus HTTPX for the API test client. |
| `.gitignore` | Excludes local database, virtual environment, Python cache, and `.env` secrets. |

## How I verified it

I ran `python -m unittest discover -s tests -v`: **5 tests passed**. The tests use temporary databases and fake GitHub responses. I generated and reopened the revised Word package to confirm that its three phase headings are present. The API imports successfully with the installed FastAPI version. I attempted a live read of the public `octocat/Hello-World` repository, but this environment could not reach GitHub (`502: GitHub request failed` from the client). The live GitHub integration remains unverified here; public repositories can be used without a token when network access is available.

## Current limits and next work

This phase **does not enforce actual GitHub pushes or merges**. `/access` evaluates policy and TCC submission checks push permission, but GitHub branch rules and a signed GitHub App/webhook flow are required for a real push gate in Phase 2. TCC is recorded after a commit is imported, so the API cannot prove the submitter authored that commit. Local bearer tokens are intended for this initial trusted-network build; GitHub OAuth and expiring sessions are Phase 2 work. Search is lexical and its answer is extractive; semantic embeddings, vector search, and model-generated grounded answers are Phase 3 work. SQLite is the Phase 1 database; migration to PostgreSQL is planned before deployment.

To run the implementation and use the interactive API docs, follow [README.md](README.md).
