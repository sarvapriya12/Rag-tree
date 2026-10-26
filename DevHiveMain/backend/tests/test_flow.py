import os
import importlib.util
import tempfile
import unittest
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DEVHIVE_SECRET", "testing-secret-at-least-32-characters-long")

from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import delete
from app.config import Settings
from app.main import app as import_app, create_app
from app.models import BranchGraphState, BranchSnapshot, UserLLMCredential, WeeklySyncRun
from app.scheduler import run_due_sync


SHA = "a" * 40


class FakeGitHub:
    def __init__(self, token):
        self.token = token

    def identity(self):
        return {"login": self.token}

    def my_repositories(self):
        return [{"owner": "team", "name": "alpha", "full_name": "team/alpha",
                 "private": True, "html_url": "https://github.com/team/alpha", "can_push": True},
                {"owner": "team", "name": "beta", "full_name": "team/beta",
                 "private": True, "html_url": "https://github.com/team/beta", "can_push": True}]

    def repository(self, owner, name):
        if owner == "other" and name in {"public", "private-shared", "private-denied"}:
            if name != "public" and (self.token is None or name == "private-denied"):
                raise HTTPException(404, "GitHub resource not found")
            return {"owner": {"login": owner}, "name": name, "full_name": f"{owner}/{name}",
                    "html_url": f"https://github.com/{owner}/{name}", "default_branch": "main",
                    "private": name != "public", "permissions": {"pull": True, "push": False}}
        return {"owner": {"login": owner}, "name": name, "full_name": f"{owner}/{name}",
                "html_url": f"https://github.com/{owner}/{name}", "default_branch": "main", "private": True,
                "permissions": {"push": True}}

    def create_repository(self, name, description, private):
        return self.repository("team", name)

    def branches(self, owner, name):
        return [{"name": branch, "commit": {"sha": SHA}} for branch in
                ("main", "feature/work", "feature/work/child")]

    def events(self, owner, name):
        return [
            {"id": "1", "type": "PushEvent", "actor": {"login": "alice"},
             "created_at": "2026-10-01T10:00:00Z", "payload": {"ref": "refs/heads/main", "size": 1, "head": SHA}},
            {"id": "2", "type": "CreateEvent", "actor": {"login": "bob"},
             "created_at": "2026-10-01T11:00:00Z", "payload": {"ref_type": "branch", "ref": "feature/work/child"}},
        ]

    def history(self, owner, name, branch, author=None):
        return [{"sha": SHA, "html_url": f"https://github.com/{owner}/{name}/commit/{SHA}",
                 "author": {"login": "bob"}, "committer": {"login": "alice"},
                 "commit": {"message": "Added feature", "committer": {"date": "2026-10-01T11:00:00Z"}}}]

    def commits(self, owner, name, branch, limit):
        return [{"sha": SHA}]

    def commit(self, owner, name, sha):
        return {"sha": sha, "parents": [], "commit": {
            "author": {"name": "Alice", "date": "2026-01-01T00:00:00Z"},
            "message": "Fix memory visibility", "tree": {"sha": SHA}},
            "files": [{"filename": "access.py", "status": "modified", "additions": 5,
                       "deletions": 1, "patch": "strict membership check"}]}

    def tree(self, owner, name, sha):
        return {"truncated": False, "tree": [{"path": "access.py", "type": "blob", "size": 42}]}

    def create_branch(self, owner, name, source, target):
        return {"ref": f"refs/heads/{target}"}

    def pull_requests(self, owner, name):
        return [self.pull_request(owner, name, 7)]

    def pull_request(self, owner, name, number):
        return {"number": number, "title": "Feature work", "head": {"ref": "feature/work",
                "repo": {"full_name": f"{owner}/{name}"}}, "base": {"ref": "main"},
                "user": {"login": "member"}, "html_url": f"https://github.com/{owner}/{name}/pull/{number}"}

    def create_pull_request(self, owner, name, head, base, title, body):
        return self.pull_request(owner, name, 7)

    def merge_pull_request(self, owner, name, number):
        return {"merged": True, "sha": SHA}

    def compare(self, owner, name, base, head):
        return {"status": "ahead" if head == "feature/work/child" else "diverged"}

    def file(self, owner, name, branch, path):
        return {"path": path, "branch": branch, "content": "strict membership check in access.py",
                "sha": SHA, "github_url": "https://github.com"}


class DevHiveFlowTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        import_app.state.engine.dispose()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = (Path(self.tmp.name) / "test.db").as_posix()
        self.db_url = f"sqlite:///{db_path}"
        self.app = create_app(Settings(self.db_url, "testing-secret-at-least-32-characters-long"))
        self.client = TestClient(self.app)
        self.auth_patch = patch("app.routes.auth.GitHubClient", FakeGitHub)
        self.repo_patch = patch("app.routes.repositories.GitHubClient", FakeGitHub)
        self.auth_patch.start(); self.repo_patch.start()
        self.owner, self.owner_headers = self.register("owner@example.com", "Owner")
        self.member, self.member_headers = self.register("member@example.com", "Member")
        self.outsider, self.outsider_headers = self.register("outsider@example.com", "Outsider")
        project = self.client.post("/projects", headers=self.owner_headers, json={"name": "Demo"})
        self.assertEqual(project.status_code, 201, project.text)
        self.project_id = project.json()["id"]
        self.client.put("/auth/github", headers=self.owner_headers, json={"token": "owner-github-token-with-many-characters"})
        roles = self.client.get(f"/projects/{self.project_id}/roles", headers=self.owner_headers).json()
        self.contributor_role = next(r["id"] for r in roles if r["name"] == "Contributor")
        self.maintainer_role = next(r["id"] for r in roles if r["name"] == "Maintainer")
        membership = self.client.put(f"/projects/{self.project_id}/members", headers=self.owner_headers,
            json={"email": self.member["email"], "role_id": self.contributor_role})
        self.assertEqual(membership.status_code, 200, membership.text)

    def tearDown(self):
        self.client.close()
        self.auth_patch.stop(); self.repo_patch.stop()
        self.app.state.engine.dispose()
        self.tmp.cleanup()

    def register(self, email, name):
        response = self.client.post("/auth/register", json={"email": email, "name": name,
            "password": "very-long-test-password"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"], {"Authorization": "Bearer " + response.json()["token"]}

    def link(self, name):
        response = self.client.post(f"/projects/{self.project_id}/repositories", headers=self.owner_headers,
            json={"github_owner": "team", "github_name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_personal_groq_keys_and_chat_are_isolated(self):
        repo_id = self.link("alpha")
        owner_key = "gsk_fake_owner_key_12345678901234567890"
        member_key = "gsk_fake_member_key_12345678901234567890"
        for headers, key in ((self.owner_headers, owner_key), (self.member_headers, member_key)):
            saved = self.client.put("/auth/llm", headers=headers, json={"api_key": key})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertNotIn(key, saved.text)
            self.assertNotIn(key, self.client.get("/auth/llm", headers=headers).text)
        self.assertFalse(self.client.get("/auth/llm", headers=self.outsider_headers).json()["configured"])
        with self.app.state.session_factory() as db:
            owner_credential = db.get(UserLLMCredential, self.owner["id"])
            member_credential = db.get(UserLLMCredential, self.member["id"])
            self.assertNotIn(owner_key, owner_credential.key_ciphertext)
            self.assertNotIn(member_key, member_credential.key_ciphertext)
            self.assertNotEqual(owner_credential.key_ciphertext, member_credential.key_ciphertext)
        sent = []

        def groq_reply(url, **kwargs):
            sent.append(kwargs)
            return httpx.Response(200, request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "This file checks access [S1]."}}]})

        with patch("app.llm.httpx.post", side_effect=groq_reply):
            for headers in (self.owner_headers, self.member_headers):
                result = self.client.post(f"/repositories/{repo_id}/file-chat", headers=headers,
                    json={"branch": "main", "path": "access.py", "message": "Explain access"})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["llm_status"], "ok")
                self.assertIn("[S1]", result.json()["summary"])
        self.assertEqual([call["headers"]["Authorization"] for call in sent],
                         [f"Bearer {owner_key}", f"Bearer {member_key}"])
        self.assertNotIn(member_key, str(sent[0]["json"]))
        self.assertNotIn(owner_key, str(sent[1]["json"]))
        self.assertEqual(sent[0]["json"]["model"], "openai/gpt-oss-120b")
        self.assertNotIn("tools", sent[0]["json"])
        self.assertEqual(self.client.post(f"/repositories/{repo_id}/file-chat",
            headers=self.outsider_headers, json={"branch": "main", "path": "access.py",
            "message": "Explain access"}).status_code, 404)
        self.assertEqual(self.client.get("/auth/llm", headers=self.outsider_headers).status_code, 200)
        self.assertEqual(self.client.delete("/auth/llm", headers=self.member_headers).status_code, 204)
        self.assertTrue(self.client.get("/auth/llm", headers=self.owner_headers).json()["configured"])
        self.assertFalse(self.client.get("/auth/llm", headers=self.member_headers).json()["configured"])

    def test_invalid_groq_key_and_provider_error_do_not_echo_secrets(self):
        alpha = self.link("alpha")
        invalid = "gsk_bad key 12345678901234567890"
        rejected = self.client.put("/auth/llm", headers=self.member_headers,
            json={"api_key": invalid})
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(invalid, rejected.text)
        key = "gsk_fake_member_key_12345678901234567890"
        self.client.put("/auth/llm", headers=self.member_headers, json={"api_key": key})
        with patch("app.llm.httpx.post", return_value=httpx.Response(401,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
            json={"error": {"message": f"bad key {key}"}})):
            result = self.client.post(f"/repositories/{alpha}/file-chat",
                headers=self.member_headers, json={"branch": "main", "path": "access.py",
                    "message": "Explain access"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["llm_status"], "unavailable")
        self.assertNotIn(key, result.text)

    def test_main_rag_chat_uses_only_callers_groq_key(self):
        alpha = self.link("alpha")
        synced = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
            params={"branch": "main"})
        self.assertEqual(synced.status_code, 200, synced.text)
        owner_key = "gsk_fake_owner_key_12345678901234567890"
        self.client.put("/auth/llm", headers=self.owner_headers, json={"api_key": owner_key})
        with patch("app.llm.httpx.post", return_value=httpx.Response(200,
            request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
            json={"choices": [{"message": {"content": "The guard checks membership [S1]."}}]})) as provider:
            owner = self.client.post(f"/repositories/{alpha}/chat", headers=self.owner_headers,
                json={"branch": "main", "message": "Show code for membership check"})
            member = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
                json={"branch": "main", "message": "Show code for membership check"})
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual(owner.json()["llm_status"], "ok")
        self.assertEqual(owner.json()["answer"], "The guard checks membership [S1].")
        self.assertEqual(member.status_code, 200, member.text)
        self.assertEqual(member.json()["llm_status"], "not_configured")
        self.assertNotEqual(member.json()["answer"], owner.json()["answer"])
        provider.assert_called_once()
        self.assertEqual(provider.call_args.kwargs["headers"]["Authorization"], f"Bearer {owner_key}")

    def test_groq_never_receives_secret_paths_or_edits_foreign_notes(self):
        alpha, beta = self.link("alpha"), self.link("beta")
        key = "gsk_fake_member_key_12345678901234567890"
        self.client.put("/auth/llm", headers=self.member_headers, json={"api_key": key})
        with patch("app.llm.httpx.post") as provider:
            result = self.client.post(f"/repositories/{alpha}/file-chat",
                headers=self.member_headers, json={"branch": "main", "path": ".env",
                    "message": "Explain this file"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["llm_status"], "no_safe_evidence")
            provider.assert_not_called()
        note = self.client.post(f"/repositories/{alpha}/file-review/notes",
            headers=self.member_headers, json={"branch": "main", "path": "access.py",
                "body": "Check the membership guard"})
        self.assertEqual(note.status_code, 201, note.text)
        note_id = note.json()["id"]
        self.assertEqual(self.client.put(f"/repositories/{beta}/file-review/notes/{note_id}",
            headers=self.member_headers, json={"body": "Wrong repo"}).status_code, 404)
        self.assertEqual(self.client.put(f"/repositories/{alpha}/file-review/notes/{note_id}",
            headers=self.outsider_headers, json={"body": "Outsider edit"}).status_code, 404)
        review = self.client.get(f"/repositories/{alpha}/file-review", headers=self.member_headers,
            params={"branch": "main", "path": "access.py"}).json()
        self.assertEqual(review["notes"][0]["body"], "Check the membership guard")

    def test_project_members_read_all_linked_repositories_and_outsiders_read_none(self):
        alpha = self.link("alpha")
        beta = self.link("beta")
        visible = self.client.get(f"/projects/{self.project_id}/repositories", headers=self.member_headers).json()
        self.assertEqual({r["id"] for r in visible}, {alpha, beta})
        self.assertEqual(self.client.get(f"/repositories/{beta}", headers=self.member_headers).status_code, 200)
        self.assertEqual(self.client.get(f"/repositories/{beta}/search", headers=self.member_headers,
                                         params={"q": "visibility"}).status_code, 200)
        self.assertEqual(self.client.get(f"/projects/{self.project_id}", headers=self.outsider_headers).status_code, 404)
        self.assertEqual(self.client.get(f"/repositories/{alpha}", headers=self.outsider_headers).status_code, 404)
        self.client.delete(f"/projects/{self.project_id}/members/{self.member['id']}", headers=self.owner_headers)
        self.assertEqual(self.client.get(f"/repositories/{alpha}", headers=self.member_headers).status_code, 404)

    def test_public_url_link_works_without_github_and_stays_view_only(self):
        project = self.client.post("/projects", headers=self.outsider_headers,
            json={"name": "Public research"})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        path = f"/projects/{project_id}/repositories/from-url"
        for url in ("https://github.com.evil/other/public", "https://github.com/other/public/issues",
                    "https://github.com/other/public?token=bad"):
            self.assertEqual(self.client.post(path, headers=self.outsider_headers,
                json={"url": url}).status_code, 422)
        linked = self.client.post(path, headers=self.outsider_headers,
            json={"url": "https://github.com/other/public"})
        self.assertEqual(linked.status_code, 201, linked.text)
        repo_id = linked.json()["id"]
        self.assertTrue(linked.json()["read_only"])
        self.assertEqual(self.client.get(f"/projects/{project_id}/repositories",
            headers=self.outsider_headers).json()[0]["id"], repo_id)
        self.assertEqual(self.client.get(f"/repositories/{repo_id}/branches",
            headers=self.outsider_headers).status_code, 200)
        self.assertEqual(self.client.get(f"/repositories/{repo_id}/file",
            headers=self.outsider_headers, params={"branch": "main", "path": "access.py"}).status_code, 200)
        synced = self.client.post(f"/repositories/{repo_id}/sync", headers=self.outsider_headers,
            params={"branch": "main"})
        self.assertEqual(synced.status_code, 200, synced.text)
        self.assertGreater(synced.json()["file_chunks"], 0)
        self.assertEqual(self.client.post(f"/repositories/{repo_id}/chat",
            headers=self.outsider_headers, json={"branch": "main",
                "message": "Show code for membership check"}).status_code, 200)
        access = self.client.get(f"/repositories/{repo_id}/access", headers=self.outsider_headers,
            params={"branch": "main"}).json()
        self.assertTrue(access["view_repository"])
        self.assertFalse(access["push"])
        self.assertFalse(access["create_branch"])
        self.assertEqual(self.client.post(f"/repositories/{repo_id}/branches",
            headers=self.outsider_headers, json={"source": "main", "name": "feature/edit"}).status_code, 403)
        self.assertEqual(self.client.post(f"/repositories/{repo_id}/pulls",
            headers=self.outsider_headers, json={"head": "feature/work", "base": "main",
                "title": "Should fail", "body": ""}).status_code, 403)
        self.assertEqual(self.client.put(f"/repositories/{repo_id}/pulls/7/merge",
            headers=self.outsider_headers).status_code, 403)
        self.assertEqual(self.client.get(f"/repositories/{repo_id}", headers=self.owner_headers).status_code, 404)
        self.assertEqual(self.client.post(path, headers=self.outsider_headers,
            json={"url": "https://github.com/other/public"}).status_code, 409)

    def test_private_url_link_requires_read_access_and_project_membership(self):
        path = f"/projects/{self.project_id}/repositories/from-url"
        self.assertEqual(self.client.post(path, headers=self.owner_headers,
            json={"url": "https://github.com/other/private-denied"}).status_code, 404)
        self.assertEqual(self.client.post(path, headers=self.member_headers,
            json={"url": "https://github.com/other/private-shared"}).status_code, 403)
        linked = self.client.post(path, headers=self.owner_headers,
            json={"url": "https://github.com/other/private-shared.git"})
        self.assertEqual(linked.status_code, 201, linked.text)
        repo_id = linked.json()["id"]
        self.assertTrue(linked.json()["read_only"])
        self.assertEqual(self.client.get(f"/repositories/{repo_id}/file",
            headers=self.member_headers, params={"branch": "main", "path": "access.py"}).status_code, 200)
        self.assertEqual(self.client.get(f"/repositories/{repo_id}/file",
            headers=self.outsider_headers, params={"branch": "main", "path": "access.py"}).status_code, 404)

    def test_activity_history_and_chat_survive_branch_write_denial(self):
        alpha = self.link("alpha")
        # Only the owner has connected GitHub; members read through that connection.
        initial = self.client.get(f"/repositories/{alpha}/activity", headers=self.member_headers)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(len(initial.json()), 2)
        self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                         params={"branch": "feature/work/child"})
        parent = self.client.put(f"/projects/{self.project_id}/branch-overrides", headers=self.owner_headers,
            json={"role_id": self.contributor_role, "pattern": "feature/work", "allow": [],
                  "deny": ["push", "create_branch"]})
        self.assertEqual(parent.status_code, 200, parent.text)
        visible_branches = self.client.get(f"/repositories/{alpha}/branches", headers=self.member_headers).json()
        self.assertEqual(len(visible_branches), 3)
        self.assertEqual([row["branch"] for row in self.client.get(
            f"/repositories/{alpha}/activity", headers=self.member_headers).json()], ["main", "feature/work/child"])
        readable = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
            json={"message": "Added feature", "branch": "feature/work/child"})
        self.assertEqual(readable.status_code, 200)
        denied = self.client.get(f"/repositories/{alpha}/access", headers=self.member_headers,
            params={"branch": "feature/work/child"})
        self.assertFalse(denied.json()["push"])
        child = self.client.put(f"/projects/{self.project_id}/branch-overrides", headers=self.owner_headers,
            json={"role_id": self.contributor_role, "pattern": "feature/work/child",
                  "allow": ["push"], "deny": []})
        self.assertEqual(child.status_code, 200, child.text)
        self.assertTrue(self.client.get(f"/repositories/{alpha}/access", headers=self.member_headers,
            params={"branch": "feature/work/child"}).json()["push"])
        visible = self.client.get(f"/repositories/{alpha}/activity", headers=self.member_headers,
                                  params={"actor": "bob", "kind": "branch_created"})
        self.assertEqual([row["branch"] for row in visible.json()], ["feature/work/child"])
        history = self.client.get(f"/repositories/{alpha}/history", headers=self.member_headers,
                                  params={"branch": "feature/work/child"})
        self.assertEqual(history.json()[0]["committer"], "alice")
        detail = self.client.get(f"/repositories/{alpha}/history/{SHA}", headers=self.member_headers,
                                 params={"branch": "feature/work/child"})
        self.assertEqual(detail.json()["files"][0]["name"], "access.py")
        self.assertEqual(self.client.get(f"/repositories/{alpha}/history/{SHA}",
            headers=self.member_headers, params={"branch": "main"}).status_code, 404)
        chat = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
            json={"message": "Added feature", "branch": "feature/work/child"})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertTrue(chat.json()["citations"])
        people = self.client.get(f"/repositories/{alpha}/contributors", headers=self.member_headers,
            params={"branch": "feature/work/child"})
        self.assertEqual(people.json()["contributors"][0]["person"], "bob")
        self.assertEqual(people.json()["contributors"][0]["recent"][0]["files"], ["access.py"])

    def test_only_owner_changes_roles_and_member_permissions(self):
        denied = self.client.put(f"/projects/{self.project_id}/roles/{self.contributor_role}",
            headers=self.member_headers, json={"name": "Contributor", "permissions": ["create_repository"]})
        self.assertEqual(denied.status_code, 403)
        denied = self.client.put(f"/projects/{self.project_id}/members", headers=self.member_headers,
            json={"email": self.outsider["email"], "role_id": self.contributor_role})
        self.assertEqual(denied.status_code, 403)
        denied = self.client.post(f"/projects/{self.project_id}/repositories", headers=self.member_headers,
            json={"github_owner": "team", "github_name": "gamma"})
        self.assertEqual(denied.status_code, 403)
        self.client.put("/auth/github", headers=self.member_headers,
                        json={"token": "member-github-token-with-many-characters"})
        allowed_role = self.client.put(f"/projects/{self.project_id}/roles/{self.contributor_role}",
            headers=self.owner_headers, json={"name": "Contributor", "permissions": [
                "view_project", "view_repository", "view_memory", "create_repository", "push", "write_tcc"]})
        self.assertEqual(allowed_role.status_code, 200, allowed_role.text)
        linked = self.client.post(f"/projects/{self.project_id}/repositories", headers=self.member_headers,
            json={"github_owner": "team", "github_name": "gamma"})
        self.assertEqual(linked.status_code, 201, linked.text)

    def test_branch_creation_and_pull_requests_without_file_editing(self):
        alpha = self.link("alpha")
        no_token = self.client.post(f"/repositories/{alpha}/branches", headers=self.member_headers,
            json={"source": "main", "name": "feature/new"})
        self.assertEqual(no_token.status_code, 409)
        self.client.put("/auth/github", headers=self.member_headers,
                        json={"token": "member-github-token-with-many-characters"})
        created = self.client.post(f"/repositories/{alpha}/branches", headers=self.member_headers,
            json={"source": "main", "name": "feature/new"})
        self.assertEqual(created.status_code, 201, created.text)
        payload = {"branch": "feature/work", "path": "access.py", "content": "new content",
                   "message": "Update access checks", "sha": SHA}
        committed = self.client.put(f"/repositories/{alpha}/file", headers=self.member_headers, json=payload)
        self.assertEqual(committed.status_code, 405, committed.text)
        payload["branch"] = "main"
        self.assertEqual(self.client.put(f"/repositories/{alpha}/file",
                         headers=self.member_headers, json=payload).status_code, 405)
        pulls = self.client.get(f"/repositories/{alpha}/pulls", headers=self.member_headers)
        self.assertEqual(pulls.json()[0]["can_merge"], False)
        proposed = self.client.post(f"/repositories/{alpha}/pulls", headers=self.member_headers,
            json={"head": "feature/work", "base": "main", "title": "Feature work"})
        self.assertEqual(proposed.status_code, 201, proposed.text)
        self.assertEqual(self.client.put(f"/repositories/{alpha}/pulls/7/merge",
                         headers=self.member_headers).status_code, 403)
        self.assertEqual(self.client.put(f"/repositories/{alpha}/pulls/7/merge",
                         headers=self.owner_headers).status_code, 200)

    def test_file_review_is_scoped_and_notes_are_editable_by_author(self):
        alpha = self.link("alpha")
        self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                         params={"branch": "main"})
        route = f"/repositories/{alpha}/file-review"
        params = {"branch": "main", "path": "access.py"}
        self.assertEqual(self.client.get(route, headers=self.outsider_headers, params=params).status_code, 404)
        created = self.client.post(f"{route}/notes", headers=self.member_headers,
            json={**params, "body": "Membership checks protect this file."})
        self.assertEqual(created.status_code, 201, created.text)
        note_id = created.json()["id"]
        self.assertEqual(self.client.put(f"{route}/notes/{note_id}", headers=self.outsider_headers,
            json={"body": "Incorrect override"}).status_code, 404)
        reviewed = self.client.get(route, headers=self.owner_headers, params=params)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["notes"][0]["author"], "Member")
        self.assertEqual(reviewed.json()["changes"][0]["message"], "Fix memory visibility")
        changed = self.client.put(f"{route}/notes/{note_id}", headers=self.member_headers,
            json={"body": "Membership checks protect repository reads."})
        self.assertEqual(changed.status_code, 200, changed.text)
        answer = self.client.post(f"/repositories/{alpha}/file-chat", headers=self.member_headers,
            json={**params, "message": "What does access.py do?"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["snippets"][0]["path"], "access.py")
        self.assertEqual(answer.json()["thoughts"][0]["author"], "Member")
        self.assertEqual(self.client.post(f"/repositories/{alpha}/file-chat", headers=self.outsider_headers,
            json={**params, "message": "What does this do?"}).status_code, 404)

    def test_file_chat_can_show_connected_code_without_leaving_repository_scope(self):
        alpha = self.link("alpha")
        tree = {"truncated": False, "tree": [
            {"path": "app/api.py", "type": "blob", "size": 55},
            {"path": "app/auth.py", "type": "blob", "size": 70},
        ]}
        def source(self_github, owner, name, branch, path):
            content = "from .auth import verify\n\ndef handle():\n    return verify()" if path == "app/api.py" else \
                "def verify():\n    return 'membership verified'"
            return {"path": path, "branch": branch, "content": content,
                    "sha": SHA, "github_url": "https://github.com/team/alpha"}
        with patch.object(FakeGitHub, "tree", return_value=tree), patch.object(FakeGitHub, "file", source):
            sync = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                    params={"branch": "main"})
            self.assertEqual(sync.status_code, 200, sync.text)
            review = self.client.get(f"/repositories/{alpha}/file-review", headers=self.member_headers,
                                     params={"branch": "main", "path": "app/api.py"})
            self.assertEqual(review.json()["imports"], ["app/auth.py"])
            with self.app.state.session_factory() as db:
                db.execute(delete(BranchGraphState).where(BranchGraphState.repository_id == alpha))
                db.commit()
            rebuilt = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                       params={"branch": "main"})
            self.assertFalse(rebuilt.json()["cached"])
            answer = self.client.post(f"/repositories/{alpha}/file-chat", headers=self.member_headers,
                json={"branch": "main", "path": "app/api.py", "message": "How does verify work?"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual([row["path"] for row in answer.json()["snippets"]],
                         ["app/api.py", "app/auth.py"])

    def test_sunday_sync_refreshes_default_and_previously_selected_branches_once(self):
        alpha = self.link("alpha")
        settings = Settings(self.db_url, "testing-secret-at-least-32-characters-long",
                            weekly_sync_enabled=True)
        before = datetime(2026, 10, 17, 20, 29, tzinfo=timezone.utc)
        due = before + timedelta(minutes=1)  # Sunday, 02:00 India time.
        factory = self.app.state.session_factory
        self.assertFalse(run_due_sync(factory, settings, before))
        self.assertTrue(run_due_sync(factory, settings, due))
        self.assertFalse(run_due_sync(factory, settings, due + timedelta(minutes=1)))
        with factory() as db:
            self.assertIsNotNone(db.get(BranchSnapshot, (alpha, "main")))
            first = db.get(WeeklySyncRun, "2026-10-18")
            self.assertEqual(first.branches_completed, 1)
            self.assertEqual(first.branches_failed, 0)
        manual = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                  params={"branch": "feature/work"})
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertTrue(run_due_sync(factory, settings, due + timedelta(days=7)))
        with factory() as db:
            second = db.get(WeeklySyncRun, "2026-10-25")
            self.assertEqual(second.branches_completed, 2)

    def test_branch_rule_only_denies_writes_and_file_snapshot_is_searchable(self):
        alpha = self.link("alpha")
        sync = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                params={"branch": "main"})
        self.assertEqual(sync.status_code, 200, sync.text)
        self.assertEqual(sync.json()["indexed_files"], 1)
        before = self.client.get(f"/repositories/{alpha}/search", headers=self.member_headers,
                                 params={"q": "visibility"})
        self.assertGreater(len(before.json()["citations"]), 0)
        self.assertTrue(any(c["kind"] == "file" for c in self.client.get(
            f"/repositories/{alpha}/search", headers=self.member_headers,
            params={"q": "access.py membership"}).json()["citations"]))
        tree = self.client.get(f"/repositories/{alpha}/tree", headers=self.member_headers,
            params={"branch": "main"})
        self.assertEqual(tree.json()["files"][0]["path"], "access.py")
        rule = self.client.put(f"/projects/{self.project_id}/branch-overrides", headers=self.owner_headers,
            json={"role_id": self.contributor_role, "pattern": "main", "allow": [],
                  "deny": ["push", "merge"]})
        self.assertEqual(rule.status_code, 200, rule.text)
        after = self.client.get(f"/repositories/{alpha}/search", headers=self.member_headers,
                                params={"q": "visibility"})
        self.assertTrue(after.json()["citations"])
        self.assertEqual(self.client.post(f"/repositories/{alpha}/tcc", headers=self.member_headers,
            json={"branch": "main", "sha": SHA, "thought": "why", "change_summary": "change",
                  "comment": "comment"}).status_code, 403)

    def test_chat_routes_code_questions_to_live_read_only_snippets(self):
        alpha = self.link("alpha")
        self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                         params={"branch": "main"})
        result = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
            json={"branch": "main", "message": "Show code for membership check"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["tool_used"], "code_lookup")
        self.assertEqual(result.json()["snippets"][0]["path"], "access.py")
        self.assertIn("membership check", result.json()["snippets"][0]["code"])
        history = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
            json={"branch": "main", "message": "Who committed the change?"})
        self.assertEqual(history.json()["tool_used"], "memory_search")
        focused = self.client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
            json={"branch": "main", "path": "access.py", "message": "Explain this file"})
        self.assertEqual(focused.json()["tool_used"], "inspect_file")
        self.assertEqual(self.client.post(f"/repositories/{alpha}/chat", headers=self.outsider_headers,
            json={"branch": "main", "message": "Show code for membership check"}).status_code, 404)

    def test_semantic_model_finds_paraphrase_and_rebuilds_cached_branch(self):
        import numpy as np

        class FakeSemanticModel:
            def encode(self, bodies, normalize_embeddings=True):
                rows = []
                for body in bodies:
                    row = np.zeros(384, dtype=np.float32)
                    row[0 if "membership" in body or "authorization gates" in body else 1] = 1
                    rows.append(row)
                return np.array(rows)

        alpha = self.link("alpha")
        first = self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                 params={"branch": "main"})
        self.assertEqual(first.status_code, 200, first.text)
        semantic_app = create_app(Settings(self.db_url,
            "testing-secret-at-least-32-characters-long", "local", "fake-semantic-model"))
        with patch("app.rag._model", return_value=FakeSemanticModel()), TestClient(semantic_app) as client:
            stale = client.get(f"/repositories/{alpha}/sync-schedule", headers=self.member_headers,
                               params={"branch": "main"})
            self.assertTrue(stale.json()["needs_reindex"])
            before = client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
                json={"branch": "main", "message": "authorization gates"})
            self.assertEqual(before.json()["citations"], [])
            rebuilt = client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                  params={"branch": "main"})
            self.assertEqual(rebuilt.status_code, 200, rebuilt.text)
            self.assertTrue(rebuilt.json()["rebuilt_embeddings"])
            self.assertFalse(rebuilt.json()["cached"])
            found = client.post(f"/repositories/{alpha}/chat", headers=self.member_headers,
                json={"branch": "main", "message": "authorization gates"})
            self.assertEqual(found.status_code, 200, found.text)
            self.assertTrue(any(c["kind"] == "file" for c in found.json()["citations"]))
            cached = client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                 params={"branch": "main"})
            self.assertFalse(cached.json()["rebuilt_embeddings"])
            self.assertTrue(cached.json()["cached"])
        semantic_app.state.engine.dispose()

    def test_tcc_immutable_and_session_revocation(self):
        alpha = self.link("alpha")
        self.client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers, params={"branch": "main"})
        payload = {"branch": "main", "sha": SHA, "thought": "permission context",
                   "change_summary": "fixed access", "comment": "reviewed"}
        first = self.client.post(f"/repositories/{alpha}/tcc", headers=self.owner_headers, json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        duplicate = self.client.post(f"/repositories/{alpha}/tcc", headers=self.owner_headers, json=payload)
        self.assertEqual(duplicate.status_code, 409)
        found = self.client.get(f"/repositories/{alpha}/search", headers=self.owner_headers,
                                params={"q": "permission context"})
        self.assertTrue(any(c["kind"] == "tcc" for c in found.json()["citations"]))
        logout = self.client.post("/auth/logout", headers=self.owner_headers)
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/auth/me", headers=self.owner_headers).status_code, 401)

    def test_github_profile_must_match_token_and_lists_eligible_repositories(self):
        token = "owner-github-token-with-many-characters"
        bad = self.client.put("/auth/github", headers=self.owner_headers,
                              json={"token": token, "profile_url": "https://github.com/someone-else"})
        self.assertEqual(bad.status_code, 422)
        good = self.client.put("/auth/github", headers=self.owner_headers,
                               json={"token": token, "profile_url": f"https://github.com/{token}"})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(self.client.get("/auth/me", headers=self.owner_headers).json()["github_login"], token)
        repos = self.client.get("/auth/github/repositories", headers=self.owner_headers)
        self.assertEqual([r["full_name"] for r in repos.json()], ["team/alpha", "team/beta"])

    def test_supabase_invitation_acceptance_reads_all_project_repositories(self):
        alpha, beta = self.link("alpha"), self.link("beta")
        invite_path = f"/projects/{self.project_id}/invitations"
        payload = {"email": "invited@example.com", "role_id": self.contributor_role}
        with patch("app.routes.projects.send_invitation") as send:
            denied = self.client.post(invite_path, headers=self.member_headers, json=payload)
            self.assertEqual(denied.status_code, 403)
            send.assert_not_called()
            invited = self.client.post(invite_path, headers=self.owner_headers, json=payload)
            self.assertEqual(invited.status_code, 201, invited.text)
            send.assert_called_once()
        self.assertEqual(len(self.client.get(invite_path, headers=self.owner_headers).json()), 1)
        verified = {"id": "supabase-person-1", "email": "invited@example.com",
                    "email_confirmed_at": "2026-10-08T00:00:00Z", "user_metadata": {"name": "Invited"}}
        with patch("app.routes.auth.verified_user", return_value=verified):
            signed_in = self.client.post("/auth/supabase", json={"access_token": "verified-token-with-enough-characters"})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        headers = {"Authorization": "Bearer " + signed_in.json()["token"]}
        self.assertEqual([p["id"] for p in self.client.get("/projects", headers=headers).json()], [self.project_id])
        self.assertEqual({r["id"] for r in self.client.get(
            f"/projects/{self.project_id}/repositories", headers=headers).json()}, {alpha, beta})
        self.assertEqual(self.client.get(f"/repositories/{beta}", headers=headers).status_code, 200)
        self.assertEqual(self.client.get(invite_path, headers=self.owner_headers).json(), [])

    def test_canceled_invitation_never_grants_project_access(self):
        alpha = self.link("alpha")
        invite_path = f"/projects/{self.project_id}/invitations"
        with patch("app.routes.projects.send_invitation"):
            invited = self.client.post(invite_path, headers=self.owner_headers, json={
                "email": "canceled@example.com", "role_id": self.contributor_role,
            })
        self.assertEqual(invited.status_code, 201, invited.text)
        self.assertEqual(self.client.get(invite_path, headers=self.member_headers).status_code, 403)
        canceled = self.client.delete(f"{invite_path}/{invited.json()['id']}", headers=self.owner_headers)
        self.assertEqual(canceled.status_code, 204, canceled.text)
        verified = {"id": "supabase-person-canceled", "email": "canceled@example.com",
                    "email_confirmed_at": "2026-10-08T00:00:00Z"}
        with patch("app.routes.auth.verified_user", return_value=verified):
            signed_in = self.client.post("/auth/supabase", json={
                "access_token": "verified-token-with-enough-characters"})
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        headers = {"Authorization": "Bearer " + signed_in.json()["token"]}
        self.assertEqual(self.client.get("/projects", headers=headers).json(), [])
        self.assertEqual(self.client.get(f"/repositories/{alpha}", headers=headers).status_code, 404)

    def test_verified_supabase_email_signs_into_existing_account(self):
        verified = {"id": "supabase-member-1", "email": self.member["email"],
                    "email_confirmed_at": "2026-10-08T00:00:00Z"}
        body = {"access_token": "verified-token-with-enough-characters"}
        with patch("app.routes.auth.verified_user", return_value=verified):
            signed_in = self.client.post("/auth/supabase", json=body)
        self.assertEqual(signed_in.status_code, 200, signed_in.text)
        self.assertEqual(signed_in.json()["user"]["id"], self.member["id"])
        headers = {"Authorization": "Bearer " + signed_in.json()["token"]}
        self.assertEqual(self.client.get(f"/projects/{self.project_id}", headers=headers).status_code, 200)

    def test_password_recovery_replaces_password_and_revokes_sessions(self):
        verified = {"id": "supabase-member-1", "email": self.member["email"],
                    "email_confirmed_at": "2026-10-08T00:00:00Z"}
        proof = "verified-recovery-token-with-enough-characters"
        with patch("app.routes.auth.verified_user", return_value=verified):
            reset = self.client.post("/auth/password/reset", json={
                "access_token": proof, "new_password": "new-very-long-test-password"})
            reused = self.client.post("/auth/password/reset", json={
                "access_token": proof, "new_password": "another-long-test-password"})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(reused.status_code, 409, reused.text)
        self.assertEqual(self.client.get("/auth/me", headers=self.member_headers).status_code, 401)
        self.assertEqual(self.client.post("/auth/login", json={
            "email": self.member["email"], "password": "very-long-test-password"}).status_code, 401)
        login = self.client.post("/auth/login", json={
            "email": self.member["email"], "password": "new-very-long-test-password"})
        self.assertEqual(login.status_code, 200, login.text)

    def test_password_recovery_uses_verified_email_instead_of_supplied_identity(self):
        verified = {"id": "supabase-other-person", "email": "missing@example.com",
                    "email_confirmed_at": "2026-10-08T00:00:00Z"}
        with patch("app.routes.auth.verified_user", return_value=verified):
            reset = self.client.post("/auth/password/reset", json={
                "access_token": "another-verified-recovery-token", "new_password": "new-very-long-test-password"})
        self.assertEqual(reset.status_code, 404)
        self.assertEqual(self.client.get("/auth/me", headers=self.member_headers).status_code, 200)

    @unittest.skipUnless(importlib.util.find_spec("turbovec"), "TurboVec optional dependency not installed")
    def test_turbovec_retrieval_includes_files_and_ignores_read_denial(self):
        alpha = self.link("alpha")
        turbo_app = create_app(Settings(self.db_url, "testing-secret-at-least-32-characters-long", "turbovec"))
        with TestClient(turbo_app) as client:
            imported = client.post(f"/repositories/{alpha}/sync", headers=self.owner_headers,
                                   params={"branch": "main"})
            self.assertEqual(imported.status_code, 200, imported.text)
            visible = client.get(f"/repositories/{alpha}/search", headers=self.member_headers,
                                 params={"q": "visibility"})
            self.assertEqual(visible.status_code, 200, visible.text)
            self.assertTrue(visible.json()["citations"])
            self.assertTrue(any(c["kind"] == "file" for c in client.post(
                f"/repositories/{alpha}/chat", headers=self.member_headers,
                json={"message": "access.py membership", "branch": "main"}).json()["citations"]))
            denied_rule = self.client.put(f"/projects/{self.project_id}/branch-overrides", headers=self.owner_headers,
                json={"role_id": self.contributor_role, "pattern": "main", "allow": [],
                      "deny": ["view_memory"]})
            self.assertEqual(denied_rule.status_code, 422)
            still_visible = client.get(f"/repositories/{alpha}/search", headers=self.member_headers,
                                       params={"q": "visibility"})
            self.assertTrue(still_visible.json()["citations"])
        turbo_app.state.engine.dispose()


if __name__ == "__main__":
    unittest.main()
