# DevHive three-phase execution plan

This plan reorganizes the supplied project description by dependencies and deliverables. It does not use the document's day estimates. GitHub remains the source of truth for code; DevHive stores access rules and engineering memory.

## Phase 1 — usable foundation (implemented here)

**Goal:** A team can register a GitHub repository, assign project roles, import commit context, and search memory within branch permissions.

1. Build a modular API and durable data model for users, projects, memberships, branch policies, commits, TCC, memory documents, and audit events.
2. Bootstrap an administrator and issue individual bearer tokens. Create users and assign per-project role levels 1–5.
3. Verify a repository through GitHub's REST API, provide a canonical link, list readable branches, preview small text files, and import recent commits and changed-file/diff metadata from a selected branch.
4. Enforce branch-specific read, push, and merge permissions with default-deny operation checks; keep owner-only membership and policy management.
5. Index imported commit metadata and immutable TCC records. Search with project and branch filters and return commit-linked citations.
6. Cover role, branch, project isolation, import idempotency, and TCC immutability with automated API tests. Document local setup and API use.

**Exit criteria:** An owner registers a real GitHub repository, adds a contributor, imports history, records TCC on an authorized branch, and both users only see memory for branches they can read. Unauthorized push/TCC and project access are rejected. The tests pass locally.

**Phase 1 boundary:** The API evaluates permissions and stores post-import TCC; it does not control GitHub's server-side push authorization. Search is lexical and extractive. These are explicit implementation limits, not claims of complete push gating or semantic RAG.

## Phase 2 — controlled developer workflow

**Goal:** Connect authorization and TCC to the actual Git workflow so a managed push cannot bypass DevHive.

1. Add GitHub OAuth identity, expiring application sessions, and secure token rotation. Map GitHub identity to project membership.
2. Build a terminal client that detects the local repository and branch, shows the applicable permissions, and captures Thought, Code/Change, and Comment before push.
3. Introduce a state machine: TCC captured → commit associated → push authorized → GitHub push confirmed → ingestion queued. Design retries and idempotency for interrupted pushes.
4. Enforce protected branch rules with GitHub branch protection/rulesets and a GitHub App or equivalent server-side check. Verify webhook signatures and reconcile webhook events with the local state machine.
5. Queue ingestion jobs so provider failures never block confirmed Git operations. Add durable delivery and retry tracking.

**Exit criteria:** A lower-role user cannot push or merge a protected branch through the managed workflow or GitHub, while an authorized developer can complete a TCC-gated push and see an immutable, commit-linked record. Restart/retry does not duplicate the record.

## Phase 3 — full engineering memory and control plane

**Goal:** Deliver the document's broader MVP experience and production readiness.

1. Move transactional storage to PostgreSQL with migrations. Add Qdrant, local BGE-M3 embeddings, and versioned chunking for commit/TCC/context documents. Apply project and branch filters inside retrieval before context assembly.
2. Add an LLM provider abstraction with grounded answers and commit/branch citations. Evaluate retrieval quality and prompt-injection behavior; retain useful keyword fallback.
3. Build a minimal web dashboard for projects, members, policies, branches, commits, TCC records, search, and audit history.
4. Add background ingestion workers, metrics, structured logs, backups, secrets management, and deployment configuration.
5. Run end-to-end and permission matrix tests against a real GitHub test repository, including revocation, private repositories, webhook replay, and failure recovery.

**Exit criteria:** A new engineer signs in, joins a project, completes a protected TCC-gated push, and another authorized engineer gets a grounded answer with commit citations. A project owner inspects members, policy decisions, and ingestion events in the dashboard. The stack can be deployed and recovered from backup.

## Dependencies and decisions

- Phase 2 depends on Phase 1's project/role model and immutable memory schema. The GitHub App/ruleset choice must be validated against actual GitHub permissions before claiming server-side enforcement.
- Phase 3 depends on the ingestion contract stabilized in Phase 2. Vector retrieval must preserve the same access checks as the current search endpoint.
- GitLab, Bitbucket, custom Git hosting, SSO/SAML, autonomous coding, live collaborative editing, multi-region deployment, and productivity scoring remain outside this plan's MVP scope.
