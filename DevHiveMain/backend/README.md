# DevHive FastAPI backend

## Local start (PowerShell)

```powershell
cd 'D:\otherAndGames\SIDE PROJECTS\Rag orchestrator\DevHiveMain\backend'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-semantic.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

For a new installation, set `DEVHIVE_SECRET` in `.env` to a random string of at least 32 characters. The current workspace already has one. Then run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

API docs: `http://127.0.0.1:8000/docs`. Users register through `/auth/register` or through the Next.js frontend. The SQLite database is created locally in this folder.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

To check retrieval against a real repository (throwaway database, real GitHub, the model and Groq settings from `.env`):

```powershell
.\.venv\Scripts\python.exe scripts/eval_repo.py          # defaults to MasteringOpenCV/code, branch master
.\.venv\Scripts\python.exe scripts/eval_repo.py --resync # reuse the eval database; forces a fresh file index
```

It imports the history, indexes the branch, asks a fixed question set and fails if an expected file is not in the top three. Run without `DEVHIVE_GROQ_API_KEY` to test retrieval alone.

The tests use temporary SQLite databases, a fake GitHub client, two fake Groq keys, and a mocked Groq response. They verify project-wide repository reads, branch write rules, file indexing, contributor history, owner-only role changes, invitations, TCC immutability, TurboVec retrieval, and per-user key isolation. Fake keys are never sent to Groq.

## Optional Supabase sign-in and invitations

Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`, and `DEVHIVE_SITE_URL` in this folder's `.env`. The secret key remains on the FastAPI server. The backend validates each Supabase access token with `/auth/v1/user`; a verified email is required. A verified Supabase identity is linked automatically to an existing DevHive account with the same email, retaining its projects and memberships. The local project owner retains sole control over roles and invitations. Invites remain pending until the recipient signs in with the invited email; project membership gives read access to every linked repository. `/auth/password/reset` accepts a verified Supabase session from an emailed recovery link, changes the local DevHive password, rejects reuse of the same access token, and revokes the user's DevHive sessions.

The `SUPABASE_SECRET_KEY` can be a current `sb_secret_...` key or a legacy service-role JWT. Configure both `${DEVHIVE_SITE_URL}/auth/callback` and `${DEVHIVE_SITE_URL}/auth/reset-password` as Supabase Auth redirect URLs. In the frontend, also configure its publishable URL/key, enable the desired GitHub and Google OAuth providers, and enable Email for password recovery. Invitations and recovery use Supabase Auth email delivery, so configure its email settings for production.

## Groq explanations: workspace key and optional personal keys

**Workspace key.** Set `DEVHIVE_GROQ_API_KEY` in `backend/.env` and every signed-in user gets AI explanations with no setup. `DEVHIVE_GROQ_MODEL` selects the model (default `openai/gpt-oss-120b`). The key stays on the server: it is never returned by any endpoint or sent to the browser. A shared key is one Groq account, so DevHive limits it:

- A per-user limit of 20 model calls per minute.
- A process-wide token budget, `DEVHIVE_GROQ_TPM` (default 7000 tokens per minute, under the 8000 limit of Groq's free tier). A request waits up to 20 seconds for budget; after that the answer falls back to the extractive summary with a "the AI is busy" note. A paid Groq plan allows a much higher `DEVHIVE_GROQ_TPM`.
- Evidence per question is capped at about 6,000 characters and 700 output tokens.

**Personal keys.** Each user may still add a Groq API key in **Settings → Connections & repositories → Groq explanations**. A personal key takes priority over the workspace key. It is encrypted in `user_llm_credentials`, indexed by that user's ID; it is never returned by GET or included in another user's request. `GET /auth/llm` reports `configured` (personal key), `shared` (workspace key present) and `active`. `PUT /auth/llm` replaces only that user's key, and `DELETE /auth/llm` removes it.

After project, branch, and file permission checks, chat retrieves bounded evidence. The backend sends the question and eligible evidence to Groq for an explanation structured as What it does, Where, History & contributors and Good to know, citing evidence cards as `[S1]`. Citation brackets are normalised to ASCII and every cited label must exist. Source labels in the answer correspond to evidence cards. Credentials, likely secret paths, and obvious hard-coded secrets are excluded from provider context. The model receives no write tools or GitHub token. Without a key, without safe evidence, or if Groq fails, the extractive answer remains available. Keys are not validated by a live Groq request when saved; the first chat reveals an invalid key through a generic unavailable state. Groq is an external processor of the selected snippets and question, so use this only with repositories whose data may be sent there. Secret detection is best effort and cannot guarantee that every sensitive string in source text is recognized.

## Link someone else's GitHub repository

A project owner, or a role granted `create_repository`, can paste `https://github.com/owner/repository` in **Settings → Connections & repositories → Link for viewing**. The backend accepts only GitHub repository URLs and resolves them through GitHub's fixed REST API endpoint. Public repositories can be linked and browsed without connecting a GitHub account. For a private repository, connect a GitHub account that already has read access; the link alone cannot grant access. Only members of the DevHive project can see the linked workspace. URL-linked repositories are view only in DevHive: branch creation, pull request creation/merge and TCC submission are denied. Review thoughts remain local to DevHive and can still be added under their usual author rules.

Public URL links never use another member's token, so a repository that later becomes private cannot be exposed through it. They use GitHub anonymously, or the optional workspace read token `DEVHIVE_GITHUB_TOKEN` (a fine-grained token with no scopes and public-repository read access only; it is never used for writes). **Set it**: anonymous reads allow only 60 GitHub requests per hour, and a first sync of a 29-commit repository costs about 35. Without it, sync imports at most five commits.

When GitHub is unreachable or rate limited, the branch list, file tree and file preview fall back to the last synced snapshot of the branch (files are rebuilt from their indexed line ranges), so browsing and "Open in explorer" keep working. Private URL links use only the account that explicitly linked them. URL-linked repository markers live in `read_only_repositories`, created automatically on startup; existing linked repositories retain their previous behavior.

## Supabase and TurboVec

1. Provision a Supabase Postgres project and obtain its direct connection string. Use a database user with permission to create the private `devhive` schema, or apply an approved migration separately. Do not add `devhive` to Supabase's exposed API schemas.
2. Install `requirements-semantic.txt`; it adds the Postgres driver, NumPy, TurboVec and sentence-transformers. The first semantic use downloads model weights. Set `HF_HOME` to a directory inside `backend` if you want the weights kept with this local installation.
3. Set `DEVHIVE_DATABASE_URL=postgresql+psycopg://...` and `DEVHIVE_RAG_BACKEND=turbovec`; start FastAPI once to create the relational tables.
4. Run `migrations/001_vectors.sql` in Supabase SQL Editor. Then set `DEVHIVE_RAG_BACKEND=supabase` for direct pgvector retrieval or `turbovec` for the process-local accelerator with SQL persistence.
5. Set `DEVHIVE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2` in `backend/.env` for semantic 384-dimensional embeddings. Restart FastAPI, then choose **Sync memory** for previously indexed branches. The sync detects an old embedding profile, re-embeds stored commit/TCC memory, and rebuilds the branch-head code snapshot even when the GitHub SHA is unchanged. The memory screen shows when a branch needs this rebuild.

The local workspace has the model cached under `backend/.cache/huggingface` and the backend `.env` enables offline loading. To verify the model yourself, run `./.venv/Scripts/python.exe scripts/check_semantic.py` from `backend`. For a fresh machine, leave the offline variables unset until the first model download succeeds.

For an existing local database, `./.venv/Scripts/python.exe scripts/sync_existing.py` refreshes branches that already had indexed memory, using the linked repository owner's GitHub connection. It records a maintenance sync in the audit log. Newly linked branches still need their first **Sync memory** in the UI.

Local SQLite with `DEVHIVE_RAG_BACKEND=turbovec` is the current development setup. Choose **Sync memory** on each branch you want searchable. Sync imports the commit history (the web UI requests up to 200 commits; the API accepts up to 500) with author, date, changed files and a bounded patch summary, fetching commit details in parallel. It then downloads the branch head as one tarball (a single GitHub request) and indexes up to `DEVHIVE_MAX_INDEX_FILES` (default 400) text files of up to 200 KB. C, C++, Objective-C, Swift, Java, Kotlin, Python, TypeScript and CMake sources are supported, and vendored folders (`*.framework`, `*.xcodeproj`, `Pods`, `node_modules`, `third_party`, ...) are skipped. Files are split along function and class boundaries into chunks of about 60 lines, and every chunk records its exact line range and nearest symbol. Older Latin-1/CP1252 sources are decoded correctly. The API returns `partial: true` only if the tree is truncated or a file was too large to index. Re-syncing reuses the stored embedding of any chunk whose text is unchanged, so only changed code is re-embedded.

Retrieval is hybrid. Each candidate chunk gets a dense similarity from the embedding model and an Okapi BM25 score over identifier-aware tokens (camelCase and snake_case are split, the whole identifier is kept, light stemming and word-prefix tokens connect `train`/`trained` and `detect`/`MarkerDetector`). The two rankings are fused with reciprocal-rank fusion, then adjusted: exact symbol or file-name matches and rare query words found in a symbol or file name rank higher; comment- or include-only chunks and bare header declarations rank lower. Permission filtering happens before ranking, and the embedding vectors and token counts are cached per repository version, so a question costs milliseconds before the model call. Without an embedding model, BM25 plus a lexical hashing vector is used.

A question always searches code and history together. The answer lists up to five code locations; each has its file, symbol, exact lines, a GitHub link at the synced commit, the commits that touched the file (author, date, sha, additions/deletions), its contributors (the same person is merged whether or not their commit had a linked GitHub login), recorded thoughts, and the files it includes or is used by (including C/C++ `#include` and Objective-C `#import`). Questions about people or history also return a branch contributor summary. Snippets come from the index, so a question makes no GitHub request. Without any Groq key, the answer is a structured Markdown summary of the same data.

Automatic memory refresh is enabled by default when FastAPI starts from `.env`. It runs **Sunday at 02:00 Asia/Kolkata time**, refreshing every linked repository's default branch and branches previously synced by hand. The backend process must be running at or after that time on Sunday. A database lease prevents duplicate runs when multiple FastAPI workers are active; failed branches are logged and can be retried manually. The next run and last indexed time appear in each repository workspace. Set `DEVHIVE_WEEKLY_SYNC_ENABLED=false` to disable it, or change `DEVHIVE_WEEKLY_SYNC_HOUR` (0–23, India time). Manual **Sync memory** remains available at any time.

DevHive checks `create_branch` and `push` on its own GitHub write endpoints and uses each writer's GitHub token. Configure GitHub repository permissions and branch rulesets as well: a user can otherwise push directly to GitHub outside DevHive. GitHub OAuth, Supabase, and any production Postgres instance need your own credentials; no live external account was used in tests.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for module ownership, permission rules and security limits.
