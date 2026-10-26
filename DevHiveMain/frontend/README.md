# DevHive Next.js frontend

## Run locally

Start the FastAPI server using [backend/README.md](../backend/README.md). In a second PowerShell terminal:

```powershell
cd 'D:\otherAndGames\SIDE PROJECTS\Rag orchestrator\DevHiveMain\frontend'
npm.cmd install
npm.cmd run db:migrate
npm.cmd run dev
```

Open `http://127.0.0.1:3000`. The local `.env` must contain a unique `NEXT_SESSION_SECRET`, `BACKEND_URL=http://127.0.0.1:8000`, and `DATABASE_URL=file:./ui.db`. Prisma stores browser sessions; FastAPI owns projects, roles, linked repositories, GitHub activity, and memory.

## Optional personal Groq explanations

In **Settings → Connections & repositories → Groq explanations**, each person can add their own [Groq API key](https://console.groq.com/keys). FastAPI encrypts it under that user's account and never shows it again. Chat sends only that person's question and eligible, accessible evidence to Groq's `openai/gpt-oss-20b` model, and displays cited source labels. Without a key or if Groq is unavailable, the extractive RAG answer still works. A saved key is not checked against Groq until the first question.

## View another person's repository

Open the project, then **Settings → Connections & repositories**. Paste a full GitHub URL such as `https://github.com/owner/repository` into **GitHub repository link** and choose **Link for viewing**. DevHive opens its read-only workspace automatically. Pick a branch to browse files, commits and activity; choose **Sync memory** before asking RAG about that branch. Public repositories need no GitHub connection. Private repositories require your connected GitHub account to have read access. Only project members can see the link inside DevHive, and only the project owner or a role with `create_repository` can add it.

## First-use workflow

1. Register or sign in, then create a project.
2. In **Settings → Connections & repositories**, connect GitHub using OAuth and link a repository. To enable the OAuth button, create a [GitHub OAuth App](https://github.com/settings/developers), set its callback to `http://127.0.0.1:3000/api/github/callback`, and set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `GITHUB_OAUTH_REDIRECT_URL` in the frontend `.env`. Restart Next.js. The OAuth App requests repository access; tokens stay server-side.
3. In **Settings → People & access**, add an existing DevHive user or invite a new one through Supabase. Every project member can read all linked repositories and branches. Only the project owner can change roles or branch rules.
4. Choose a branch and click **Sync memory** for its first index. The result shows imported commits, searchable file sections, and whether the file snapshot was partial. The backend automatically refreshes the default branch and previously selected branches every Sunday at 02:00 India time while FastAPI is running. The workspace shows the next run and last indexed time; manual sync remains available.
5. Open **Memory** for the full-width branch chat. Code questions fetch live, read-only GitHub snippets; the evidence panel shows code and cited memory. Select a file in Repositories first if you want to turn on file focus in Memory. **Activity** shows contributors, recent events, live commits, and changed files. Select a person or branch to filter.
6. Open a repository card to jump to its workspace. The file tree and syntax-colored code preview are read only. Choose a file to review its import links, recent changes and team thoughts. Add or edit your own thought, and use **Ask file memory** for a response focused on that file; related source appears when an indexed import link is relevant. A role with `create_branch` may create a branch. A role with `push` may open a pull request using that person's own GitHub connection, and a role with `merge` on the target branch may merge it. GitHub must also grant the relevant write access. Sync memory after changes on GitHub.

To enable Supabase sign-in, invitations, and password recovery, set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` in the frontend `.env`, and the matching URL, publishable key, secret key, and site URL in the backend `.env`. Add both `http://127.0.0.1:3000/auth/callback` and `http://127.0.0.1:3000/auth/reset-password` to Supabase Auth redirect URLs. Enable Google for direct Google sign-in and Email for recovery links. Restart both servers. A verified Supabase email signs into the existing DevHive account with the same email and keeps its project access. On the sign-in page, **Forgot password?** sends a Supabase email link; open it in the same browser to set a new DevHive password. Password recovery ends existing DevHive sessions. Supabase's default email delivery has low rate limits, so configure SMTP for production.

DevHive's branch rules control writes through DevHive. Configure GitHub branch rulesets if direct GitHub pushes must follow the same policy. GitHub's Events API is a delayed recent feed, and the contributor summary covers the latest 100 commits on a branch. File indexing is bounded to 60 eligible small text files and at most eight sections per file. The local default embedding is lexical; configure the backend semantic model for paraphrase-style questions, then sync each branch again.

Verify the frontend with `npm.cmd run build`. The workspace UI is in `components/Workspace.tsx`; personal key settings are in `components/GroqSettings.tsx`; the full-width memory screen is in `components/MemoryWorkspace.tsx`; the read-only tree and review panel are in `components/FileReview.tsx`, and code formatting is in `components/CodeView.tsx`. Activity is in `components/GitHubActivity.tsx`; authenticated backend routing is in `app/api/backend/[...path]/route.ts`.
