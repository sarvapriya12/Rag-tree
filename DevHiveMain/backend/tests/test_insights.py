import os
import re
import tempfile
import time
import unittest
import httpx
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DEVHIVE_SECRET", "testing-secret-at-least-32-characters-long")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import llm
from app.config import Settings
from app.main import create_app
from app.models import FileChunk
from app.retrieval import budget_windows, folder_digests, resolve_profile
from test_flow import FakeGitHub, SHA

SECRET = "testing-secret-at-least-32-characters-long"
OLD, NEW = "b" * 40, "c" * 40
CPP = "\n".join([
    '#include "detect.h"',
    "",
    "// Detects faces in a frame using a Haar cascade.",
    "int detectFaces(Mat frame, Mat gray)",
    "{",
    *[f"    int step{i} = {i};  // cascade work" for i in range(60)],
    "    return 0;",
    "}",
    "",
    "void drawBoxes(Mat frame)",
    "{",
    *[f"    rectangle(frame, {i});" for i in range(30)],
    "}",
])
HEADER = "int detectFaces(Mat frame, Mat gray);\nvoid drawBoxes(Mat frame);"
OUTAGE = HTTPException(502, "Public GitHub rate limit reached")


class InsightGitHub(FakeGitHub):
    """Two commits by one person (one with a linked login, one without) and a C++ tree."""
    down = False

    def branches(self, owner, name):
        if self.down:
            raise OUTAGE
        return super().branches(owner, name)

    def commits(self, owner, name, branch, limit):
        return [{"sha": NEW}, {"sha": OLD}]

    def commit(self, owner, name, sha):
        newest = sha == NEW
        return {"sha": sha, "parents": [{"sha": OLD}] if newest else [],
                "author": {"login": "alice-dev"} if newest else None,
                "commit": {"author": {"name": "Alice", "date": "2026-02-01T00:00:00Z" if newest else "2026-01-01T00:00:00Z"},
                           "message": "Speed up face detection" if newest else "Add face detector", "tree": {"sha": SHA}},
                "files": [{"filename": "src/detect.cpp", "status": "modified", "additions": 9 if newest else 90,
                           "deletions": 2 if newest else 0, "patch": "@@ detect"}]}

    def tree(self, owner, name, sha):
        if self.down:
            raise OUTAGE
        return {"truncated": False, "tree": [
            {"path": "src/detect.cpp", "type": "blob", "size": len(CPP)},
            {"path": "src/detect.h", "type": "blob", "size": len(HEADER)},
            {"path": "ios/Pods/Vendor/vendor.cpp", "type": "blob", "size": 20},
            {"path": "ios/opencv2.framework/Headers/core.hpp", "type": "blob", "size": 20}]}

    def archive(self, owner, name, sha):
        return {"src/detect.cpp": CPP, "src/detect.h": HEADER, "ios/Pods/Vendor/vendor.cpp": "int vendor();",
                "ios/opencv2.framework/Headers/core.hpp": "class Mat;"}

    def file(self, owner, name, branch, path):
        if self.down:
            raise OUTAGE
        return super().file(owner, name, branch, path)


class InsightBase(unittest.TestCase):
    """Registers an owner, links team/alpha and syncs main against `github_class`."""
    github_class = InsightGitHub

    def setUp(self):
        llm._spent.clear()
        llm._calls.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_url = f"sqlite:///{(Path(self.tmp.name) / 'test.db').as_posix()}"
        InsightGitHub.down = False
        self.patches = [patch("app.routes.auth.GitHubClient", self.github_class),
                        patch("app.routes.repositories.GitHubClient", self.github_class)]
        for item in self.patches:
            item.start()
        self.app = create_app(Settings(self.db_url, SECRET))
        self.client = TestClient(self.app)
        self.owner, self.owner_headers = self.register("owner@example.com", "Owner")
        project = self.client.post("/projects", headers=self.owner_headers, json={"name": "Demo"}).json()
        self.client.put("/auth/github", headers=self.owner_headers, json={"token": "owner-github-token-with-many-characters"})
        self.repo_id = self.client.post(f"/projects/{project['id']}/repositories", headers=self.owner_headers,
            json={"github_owner": "team", "github_name": "alpha"}).json()["id"]
        synced = self.client.post(f"/repositories/{self.repo_id}/sync", headers=self.owner_headers,
                                  params={"branch": "main", "limit": 50})
        self.assertEqual(synced.status_code, 200, synced.text)
        self.sync = synced.json()

    def tearDown(self):
        self.client.close()
        for item in self.patches:
            item.stop()
        self.app.state.engine.dispose()
        self.tmp.cleanup()

    def register(self, email, name):
        response = self.client.post("/auth/register", json={"email": email, "name": name,
            "password": "very-long-test-password"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"], {"Authorization": "Bearer " + response.json()["token"]}

    def chat(self, message, client=None, headers=None):
        reply = (client or self.client).post(f"/repositories/{self.repo_id}/chat",
            headers=headers or self.owner_headers, json={"branch": "main", "message": message})
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()


class InsightTest(InsightBase):
    def test_sync_indexes_cpp_with_exact_line_ranges_and_skips_vendored(self):
        self.assertEqual(self.sync["imported"], 2)
        self.assertEqual(self.sync["indexed_files"], 2)
        self.assertFalse(self.sync["partial"])
        with self.app.state.session_factory() as db:
            chunks = db.execute(select(FileChunk)).scalars().all()
            self.assertEqual({c.path for c in chunks}, {"src/detect.cpp", "src/detect.h"})
            lines = CPP.splitlines()
            for chunk in (c for c in chunks if c.path == "src/detect.cpp"):
                self.assertEqual(chunk.body.split("\n", 1)[1], "\n".join(lines[chunk.start_line - 1:chunk.end_line]))
            self.assertIn("detectFaces", {c.symbol for c in chunks})

    def test_chat_returns_located_code_with_history_and_merged_contributors(self):
        answer = self.chat("Who changed detectFaces and when?")
        top = answer["files"][0]
        self.assertEqual(top["path"], "src/detect.cpp")
        self.assertEqual(top["symbol"], "detectFaces")
        self.assertTrue(top["github_url"].startswith("https://github.com/team/alpha/blob/"))
        self.assertRegex(top["github_url"], r"#L\d+-L\d+$")
        self.assertEqual([c["message"] for c in top["history"]], ["Speed up face detection", "Add face detector"])
        self.assertEqual(top["history"][0]["additions"], 9)
        self.assertTrue(top["history"][0]["url"].endswith(f"/commit/{NEW}"))
        # "Alice" and "Alice (@alice-dev)" are the same person.
        self.assertEqual(top["contributors"], [{"person": "Alice (@alice-dev)", "commits": 2,
                                               "last_at": "2026-02-01T00:00:00Z"}])
        self.assertEqual(len(answer["team"]), 1)
        self.assertEqual(answer["team"][0]["commits"], 2)
        self.assertIn("Alice", answer["answer"])

    def test_workspace_key_answers_for_everyone_and_personal_key_wins(self):
        shared = "gsk_shared_workspace_key_1234567890abcdef"
        personal = "gsk_personal_member_key_1234567890abcdef"
        # This test is about which key answers, so give it room for three questions in one minute.
        shared_app = create_app(Settings(self.db_url, SECRET, groq_api_key=shared,
                                         groq_model="openai/gpt-oss-120b", groq_tpm=60000))
        reply = httpx.Response(200, request=httpx.Request("POST", llm.GROQ_URL),
                               json={"choices": [{"message": {"content": "It detects faces 【S1†L1】."}}]})
        with TestClient(shared_app) as client, patch("app.llm.httpx.post", return_value=reply) as provider:
            state = client.get("/auth/llm", headers=self.owner_headers).json()
            self.assertEqual((state["shared"], state["configured"], state["active"]), (True, False, True))
            first = self.chat("How does detectFaces work?", client=client)
            self.assertEqual(first["llm_status"], "ok")
            self.assertIn("[S1]", first["answer"])  # fancy brackets are normalised for the UI
            self.assertEqual(provider.call_args.kwargs["headers"]["Authorization"], f"Bearer {shared}")
            self.assertNotIn(shared, str(first))
            client.put("/auth/llm", headers=self.owner_headers, json={"api_key": personal})
            self.chat("How does detectFaces work?", client=client)
            self.assertEqual(provider.call_args.kwargs["headers"]["Authorization"], f"Bearer {personal}")
            self.assertTrue(client.get("/auth/llm", headers=self.owner_headers).json()["configured"])
            client.delete("/auth/llm", headers=self.owner_headers)
            self.chat("How does detectFaces work?", client=client)
            self.assertEqual(provider.call_args.kwargs["headers"]["Authorization"], f"Bearer {shared}")
        shared_app.state.engine.dispose()

    def test_without_any_key_the_answer_is_still_a_structured_summary(self):
        answer = self.chat("Where is detectFaces implemented?")
        self.assertEqual(answer["llm_status"], "not_configured")
        self.assertIn("**Closest match on `main`:**", answer["answer"])
        self.assertIn("src/detect.cpp", answer["answer"])

    def test_citation_variants_become_ascii_chips(self):
        self.assertEqual(llm.normalize_citations("a [ S1 ] b 【S2†L3】 c [S3, S4] d [link](x)"),
                         "a [S1] b [S2] c [S3][S4] d [link](x)")

    def test_shared_token_budget_waits_then_refuses(self):
        self.assertTrue(llm._reserve_tokens(900, 1000))
        with patch.object(llm, "MAX_WAIT_SECONDS", 0):
            self.assertFalse(llm._reserve_tokens(900, 1000))
        self.assertTrue(llm._reserve_tokens(50, 1000))

    def test_explorer_falls_back_to_the_synced_snapshot_when_github_is_down(self):
        InsightGitHub.down = True
        branches = self.client.get(f"/repositories/{self.repo_id}/branches", headers=self.owner_headers)
        self.assertEqual([b["name"] for b in branches.json()], ["main"])
        tree = self.client.get(f"/repositories/{self.repo_id}/tree", headers=self.owner_headers,
                               params={"branch": "main"}).json()
        self.assertEqual([f["path"] for f in tree["files"]], ["src/detect.cpp", "src/detect.h"])
        self.assertTrue(tree["indexed_snapshot"])
        opened = self.client.get(f"/repositories/{self.repo_id}/file", headers=self.owner_headers,
                                 params={"branch": "main", "path": "src/detect.cpp"}).json()
        self.assertEqual(opened["content"], CPP)
        self.assertTrue(opened["indexed_snapshot"])
        missing = self.client.get(f"/repositories/{self.repo_id}/file", headers=self.owner_headers,
                                  params={"branch": "main", "path": "src/other.cpp"})
        self.assertEqual(missing.status_code, 502)


SOURCES = {
    "app/api.py": "from .auth import verify\n\ndef handle_request():\n    return verify()\n",
    "app/auth.py": "def verify():\n    return 'membership verified'\n",
    "docs/README.md": "# Docs\nThe guide explains deployment steps for the service.\n",
    "lib/util.py": "def helper():\n    return 'shared helper for parsing'\n",
    "a_b/x.py": "def underscore_folder():\n    return 1\n",
    "axb/x.py": "def other_folder():\n    return 2\n",
}


class MultiFolderGitHub(InsightGitHub):
    """Six files in five folders; the newest commit touched app/, the older one lib/."""

    def commit(self, owner, name, sha):
        newest = sha == NEW
        return {"sha": sha, "parents": [{"sha": OLD}] if newest else [], "author": {"login": "alice-dev"},
                "commit": {"author": {"name": "Alice", "date": "2026-02-01T00:00:00Z" if newest else "2026-01-01T00:00:00Z"},
                           "message": "Tighten auth checks" if newest else "Add parsing helper",
                           "tree": {"sha": SHA}},
                "files": [{"filename": "app/api.py" if newest else "lib/util.py", "status": "modified",
                           "additions": 3, "deletions": 1, "patch": "@@ change"}]}

    def tree(self, owner, name, sha):
        blobs = [{"path": path, "type": "blob", "size": len(text)} for path, text in SOURCES.items()]
        return {"truncated": False, "tree": blobs + [{"path": "app/logo.png", "type": "blob", "size": 900}]}

    def archive(self, owner, name, sha):
        return dict(SOURCES)

    def file(self, owner, name, branch, path):
        return {"path": path, "branch": branch, "content": SOURCES.get(path, ""), "sha": SHA,
                "github_url": "https://github.com"}


class ScopedChatTest(InsightBase):
    github_class = MultiFolderGitHub

    def ask(self, message="What does this do?", expect=200, headers=None, **extra):
        reply = self.client.post(f"/repositories/{self.repo_id}/chat", headers=headers or self.owner_headers,
                                 json={"branch": "main", "message": message, **extra})
        self.assertEqual(reply.status_code, expect, reply.text)
        return reply.json()

    def test_scope_limits_code_and_history_to_the_selection(self):
        app = self.ask("Who tightened the auth checks in verify?", scope=["app"], depth="high")
        self.assertTrue(app["files"])
        self.assertTrue(all(f["path"].startswith("app/") for f in app["files"]), app["files"])
        self.assertEqual(app["scope"], ["app"])
        self.assertEqual(app["depth"], "high")
        self.assertEqual({c["sha"] for c in app["citations"] if c["kind"] != "file"}, {NEW})
        lib = self.ask("Who added the parsing helper?", scope=["lib"])
        self.assertEqual({f["path"] for f in lib["files"]}, {"lib/util.py"})
        self.assertEqual({c["sha"] for c in lib["citations"] if c["kind"] != "file"}, {OLD})
        stats = app["retrieval"]
        self.assertEqual((stats["folders_in_scope"], stats["folders_covered"]), (1, 1))
        self.assertEqual(stats["llm_calls"], 0)

    def test_underscore_and_percent_in_a_scope_are_literal(self):
        answer = self.ask("What does the folder contain?", scope=["a_b"])
        self.assertEqual({f["path"] for f in answer["files"]}, {"a_b/x.py"})

    def test_one_file_scope_inspects_the_file_and_its_neighbours(self):
        answer = self.ask("How does handle_request verify?", scope=["app/api.py"])
        self.assertEqual(answer["tool_used"], "inspect_file")
        paths = [f["path"] for f in answer["files"]]
        self.assertEqual(paths[0], "app/api.py")
        self.assertIn("app/auth.py", paths)
        self.assertEqual(next(f for f in answer["files"] if f["path"] == "app/auth.py")["group"], "linked files")
        legacy = self.ask("How does handle_request verify?", path="app/api.py")
        self.assertEqual(legacy["tool_used"], "inspect_file")

    def test_every_depth_covers_every_folder_in_scope(self):
        folders = {"app", "docs", "lib", "a_b", "axb"}
        for depth in ("low", "medium", "high"):
            answer = self.ask("What does each folder of this repository do?", depth=depth)
            stats = answer["retrieval"]
            self.assertEqual((stats["folders_in_scope"], stats["folders_covered"], stats["folders_with_code"]),
                             (5, 5, 5), depth)
            self.assertEqual({f["group"] for f in answer["files"]}, folders, depth)
            self.assertEqual({row["path"] for row in answer["outline"]["folders"]}, folders)
            self.assertTrue(all(row["excerpts"] for row in answer["outline"]["folders"]))
            self.assertIn("**Coverage:** 5 of 5 folders", answer["answer"])
        # Deeper levels may read more sections per folder (app holds two files).
        low = self.ask("What does each folder of this repository do?", depth="low")
        high = self.ask("What does each folder of this repository do?", depth="high")
        self.assertEqual(sum(f["group"] == "app" for f in low["files"]), 1)
        self.assertEqual(sum(f["group"] == "app" for f in high["files"]), 2)

    def test_a_pointed_question_focuses_but_still_lists_every_folder(self):
        pointed = self.ask("Where is underscore_folder defined?", depth="medium")
        self.assertEqual(pointed["files"][0]["path"], "a_b/x.py")
        self.assertEqual(len(pointed["outline"]["folders"]), 5)
        self.assertEqual(pointed["retrieval"]["folders_covered"], 5)
        self.assertLess(pointed["retrieval"]["folders_with_code"], 5)
        self.assertIn("summary only", pointed["answer"])

    def test_invalid_scope_depth_and_outsiders_are_rejected(self):
        for bad in (["../secret"], ["a\\b"], ["a//b"]):
            self.ask(scope=bad, expect=422)
        self.ask(scope=[f"folder{i}" for i in range(51)], expect=422)
        self.ask(depth="extreme", expect=422)
        _, outsider = self.register("outsider@example.com", "Outsider")
        self.ask(scope=["app"], expect=404, headers=outsider)

    def test_an_empty_scope_match_answers_without_calling_anything(self):
        answer = self.ask(scope=["no/such/folder"])
        self.assertEqual((answer["files"], answer["retrieval"]["windows"]), ([], 0))
        self.assertIn("Nothing is indexed", answer["answer"])

    def test_very_high_runs_as_a_polled_job_that_only_its_owner_can_read(self):
        base = f"/repositories/{self.repo_id}/chat/jobs"
        started = self.client.post(base, headers=self.owner_headers, json={
            "branch": "main", "message": "What does each folder do?", "depth": "very_high"})
        self.assertEqual(started.status_code, 202, started.text)
        job = started.json()["job_id"]
        _, outsider = self.register("outsider@example.com", "Outsider")
        self.assertEqual(self.client.get(f"{base}/{job}", headers=outsider).status_code, 404)
        state = {}
        for _ in range(100):
            state = self.client.get(f"{base}/{job}", headers=self.owner_headers).json()
            if state["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.1)
        self.assertEqual(state["status"], "done", state)
        self.assertTrue(state["result"]["files"])
        self.assertEqual(state["result"]["depth"], "very_high")
        self.assertTrue(state["result"]["retrieval"]["map_reduce"])
        self.assertEqual(self.client.delete(f"{base}/{job}", headers=self.owner_headers).status_code, 204)
        self.assertEqual(self.client.delete(f"{base}/{job}", headers=outsider).status_code, 404)
        self.assertEqual(self.client.get(f"{base}/missing", headers=self.owner_headers).status_code, 404)
        invalid = self.client.post(base, headers=self.owner_headers, json={
            "branch": "main", "message": "What does it do?", "scope": ["../x"], "depth": "very_high"})
        self.assertEqual(invalid.status_code, 422)

    def test_scope_tree_lists_only_indexed_files_with_section_counts(self):
        route = f"/repositories/{self.repo_id}/scope-tree"
        tree = self.client.get(route, headers=self.owner_headers, params={"branch": "main"}).json()
        self.assertEqual([f["path"] for f in tree["files"]], sorted(SOURCES))
        self.assertTrue(all(f["chunks"] >= 1 for f in tree["files"]))
        self.assertEqual((tree["total_files"], tree["profile_ready"]), (len(SOURCES), True))
        self.assertEqual(tree["total_chunks"], sum(f["chunks"] for f in tree["files"]))
        _, outsider = self.register("outsider@example.com", "Outsider")
        self.assertEqual(self.client.get(route, headers=outsider, params={"branch": "main"}).status_code, 404)

    def test_explorer_tree_marks_which_files_are_indexed(self):
        tree = self.client.get(f"/repositories/{self.repo_id}/tree", headers=self.owner_headers,
                               params={"branch": "main"}).json()
        flags = {f["path"]: f["indexed"] for f in tree["files"]}
        self.assertEqual(flags["app/api.py"], True)
        self.assertEqual(flags["app/logo.png"], False)


class StubDb:
    def get(self, model, key):
        return None  # no personal key: the workspace key answers


class StubUser:
    id = "stub-user"


def window(index, group, code_chars=1800):
    return {"path": f"{group}/file{index}.cpp", "symbol": f"fn{index}", "start_line": 1, "end_line": 40,
            "code": ("int x = %d; // %s\n" % (index, group)) * (code_chars // 24), "group": group,
            "score": 1.0 - index * 0.01, "sections": 1, "history": [], "contributors": [],
            "imports": [], "used_by": [], "focus_start": 1, "focus_end": 10}


def groq(content_for):
    """A fake httpx.post: content_for(system, user) -> text. Records every payload."""
    sent = []

    def post(url, **kwargs):
        payload = kwargs["json"]
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        sent.append({"system": system, "user": user, "payload": payload})
        reply = content_for(system, user)
        if isinstance(reply, int):
            return httpx.Response(reply, request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"choices": [{"message": {"content": reply}}]})
    return post, sent


class DepthLlmTest(unittest.TestCase):
    def setUp(self):
        llm._spent.clear()
        llm._calls.clear()
        self.settings = Settings("sqlite://", SECRET, groq_api_key="gsk_shared_workspace_key_1234567890abcdef",
                                 groq_tpm=60000)

    def explain(self, post, result, depth, progress=None):
        with patch("app.llm.httpx.post", side_effect=post):
            return llm.explain(StubDb(), self.settings, StubUser(), "What does each folder do?", result,
                               resolve_profile(depth, self.settings.groq_tpm), progress)

    def broad_result(self, folders=6, per_folder=2):
        files = [window(i, f"Chapter{i % folders + 1}") for i in range(folders * per_folder)]
        return {"branch": "main", "files": files, "citations": [], "thoughts": [],
                "outline": {"text": "Repository outline (indexed files by folder):\n- Chapter1: 3 files"}}

    def test_depth_changes_completion_effort_and_length_guidance(self):
        sizes = {}
        for depth, completion, effort, note in (("low", 800, "low", "one or two sentences per folder"),
                                                ("high", 1500, "low", "a subsection per folder")):
            post, sent = groq(lambda system, user: "It parses input [S2].")
            text, status = self.explain(post, self.broad_result(), depth)
            self.assertEqual(status, "ok", depth)
            self.assertEqual(sent[0]["payload"]["max_completion_tokens"], completion)
            self.assertEqual(sent[0]["payload"]["reasoning_effort"], effort)
            self.assertIn(note, sent[0]["system"])
            self.assertIn("Cover every folder", sent[0]["system"])
            sizes[depth] = len(sent[0]["user"])
        self.assertLess(sizes["low"], sizes["high"])

    def test_every_window_reaches_the_model_instead_of_the_first_few(self):
        result = self.broad_result(folders=4, per_folder=2)
        post, sent = groq(lambda system, user: "Overview [S1].")
        self.explain(post, result, "low")
        for label in (f"S{n}" for n in range(1, 10)):  # outline + 8 windows
            self.assertIn(f"[{label}]", sent[0]["user"])
        self.assertEqual([row["source_label"] for row in result["files"]], [f"S{n}" for n in range(2, 10)])
        self.assertEqual(result["outline"]["source_label"], "S1")

    def test_very_high_summarises_each_batch_then_synthesises_with_global_labels(self):
        result = self.broad_result()
        stages = []

        def content(system, user):
            if system == llm.MAP_PROMPT:
                labels = re.findall(r"\[(S\d+)\]", user)
                return f"- Handles input [{labels[0]}][S99]"  # S99 is outside the batch
            return "### Chapter1\nParses input [S2].\n\n**Synthesis** All chapters [S3]."

        post, sent = groq(content)
        text, status = self.explain(post, result, "very_high", lambda *args: stages.append(args))
        self.assertEqual(status, "ok")
        maps = [call for call in sent if call["system"] == llm.MAP_PROMPT]
        reduces = [call for call in sent if call["system"] != llm.MAP_PROMPT]
        self.assertGreaterEqual(len(maps), 2)
        self.assertEqual(len(reduces), 1)
        self.assertEqual(result["llm_calls"], len(maps) + 1)
        # Each source label is summarised by exactly one map call.
        seen = [set(re.findall(r"\[(S\d+)\]", call["user"])) for call in maps]
        self.assertEqual(len(set().union(*seen)), sum(len(s) for s in seen))
        self.assertEqual(set().union(*seen), {f"S{n}" for n in range(2, 14)})  # S1 is the outline
        for note in result["groups"]:
            self.assertNotIn("S99", note["summary"])
            self.assertTrue(note["labels"])
        self.assertIn("Notes for", reduces[0]["user"])
        self.assertIn("Repository outline", reduces[0]["user"])
        self.assertEqual(reduces[0]["payload"]["max_completion_tokens"], 2100)
        self.assertRegex(reduces[0]["system"], r"about \d+ words each")
        self.assertEqual([stage[0] for stage in stages], ["map"] * len(maps) + ["reduce"])

    def test_a_failed_synthesis_falls_back_to_the_folder_notes(self):
        def content(system, user):
            return "- Handles input [%s]" % re.findall(r"\[(S\d+)\]", user)[0] if system == llm.MAP_PROMPT else 500

        post, _ = groq(content)
        text, status = self.explain(post, self.broad_result(), "very_high")
        self.assertEqual(status, "partial")
        self.assertIn("### Chapter", text)
        self.assertRegex(text, r"\[S\d+\]")

    def test_small_evidence_skips_the_split_and_uses_one_call(self):
        result = {"branch": "main", "files": [window(1, "Chapter1", 600), window(2, "Chapter2", 600)],
                  "citations": [], "thoughts": []}
        post, sent = groq(lambda system, user: "Two folders [S1][S2].")
        text, status = self.explain(post, result, "very_high")
        self.assertEqual((status, len(sent), result["llm_calls"]), ("ok", 1, 1))

    def test_cancelling_between_calls_stops_the_remaining_ones(self):
        post, sent = groq(lambda system, user: "- note [%s]" % re.findall(r"\[(S\d+)\]", user)[0])

        def progress(stage, step, total, message):
            if step == 1:
                raise RuntimeError("cancelled")

        with self.assertRaises(RuntimeError):
            self.explain(post, self.broad_result(), "very_high", progress)
        self.assertEqual(len(sent), 1)

    def test_forty_folders_at_low_stay_in_budget_and_all_are_named(self):
        profile = resolve_profile("low", self.settings.groq_tpm)
        chunks = [FileChunk(id=i + 1, repository_id="r", branch="main", head_sha="a" * 40, path=f"pkg{i:02d}/m.cpp",
                            segment=0, start_line=1, end_line=25, symbol=f"run{i}", body=f"pkg{i:02d}/m.cpp\ncode",
                            embedding_json="[0.0]") for i in range(40)]
        relevance = {f"pkg{i:02d}": 1.0 - i * 0.02 for i in range(40)}
        digest = folder_digests(chunks, [], relevance, {}, profile)
        windows = [dict(window(i, f"pkg{i:02d}"), path=f"pkg{i:02d}/m.cpp", extras=True) for i in range(40)]
        kept = budget_windows(windows, relevance, profile, len(digest["text"]))
        result = {"branch": "main", "files": kept, "citations": [], "thoughts": [], "outline": digest}
        post, sent = groq(lambda system, user: "Covered [S1].")
        with patch("app.llm.httpx.post", side_effect=post):
            text, status = llm.explain(StubDb(), self.settings, StubUser(), "What does each package do?", result, profile)
        self.assertEqual(status, "ok")
        evidence = sent[0]["user"].split("Authorized evidence:\n", 1)[1]
        self.assertLessEqual(len(evidence), profile["evidence_chars"])
        for i in range(40):
            self.assertIn(f"pkg{i:02d}", evidence)  # every folder is named by its digest line
        self.assertIn("pkg00/m.cpp", evidence)  # code for the most relevant folders
        self.assertLess(len(kept), 40)

    def overview(self):
        folders = [{"path": f"Chapter{n}_Demo", "files": 3, "chunks": 9, "readme": "", "symbols": [f"run{n}"]}
                   for n in (1, 2, 3)]
        return {"branch": "main", "broad": True, "citations": [], "thoughts": [],
                "files": [window(1, "Chapter1_Demo", 600)],
                "outline": {"text": "Folders in scope:\n- Chapter1_Demo\n- Chapter2_Demo\n- Chapter3_Demo", "folders": folders}}

    def test_an_overview_answer_lists_every_folder_the_model_skipped(self):
        result = self.overview()
        post, sent = groq(lambda system, user: "Chapter 1 cartoonifies frames [S2].")
        text, status = self.explain(post, result, "low")
        self.assertEqual(status, "ok")
        self.assertIn("**Also in scope**", text)
        self.assertIn("Chapter2_Demo", text)
        self.assertIn("Chapter3_Demo", text)
        self.assertNotIn("**Chapter1_Demo**", text)  # "Chapter 1" already covered it
        self.assertIn("[S1]", text.split("**Also in scope**")[1])  # cited to the digest
        self.assertRegex(sent[0]["system"], r"about \d+ words each")

    def test_folder_names_are_recognised_however_the_model_spaces_them(self):
        for text in ("Chapter5_NumberPlate does OCR", "Chapter 5 does OCR", "Chapter 5 does OCR",
                     "chapter5: OCR", "Chapter‑5 does OCR"):
            self.assertTrue(llm._mentions(text, "Chapter5_NumberPlate"), repr(text))
        self.assertFalse(llm._mentions("Chapter 15 does OCR", "Chapter5_NumberPlate"))
        self.assertFalse(llm._mentions("Chapter 50", "Chapter5_NumberPlate"))

    def test_word_budget_per_folder_shrinks_as_folders_grow(self):
        few = llm._depth_note("high", 1500, "medium", 2)
        many = llm._depth_note("high", 1500, "medium", 20)
        words = [int(re.search(r"about (\d+) words", note).group(1)) for note in (few, many)]
        self.assertGreater(words[0], words[1])
        self.assertGreaterEqual(words[1], 25)

    def test_a_truncated_answer_drops_its_half_written_paragraph(self):
        def post(url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{
                "finish_reason": "length", "message": {"content":
                    "Chapter1, Chapter2 and Chapter3 parse input [S2].\n\n---\n\n## Chapter4\nHalf wri"}}]})
        text, status = self.explain(post, self.overview(), "low")
        self.assertEqual(status, "ok")
        self.assertEqual(text, "Chapter1, Chapter2 and Chapter3 parse input [S2].")

    def test_answers_over_several_folders_use_one_compact_section_each(self):
        post, sent = groq(lambda system, user: "Chapter1, Chapter2 and Chapter3 parse input [S2].")
        self.explain(post, self.overview(), "high")
        self.assertIn("### <folder name>", sent[0]["system"])
        self.assertNotIn("**History & contributors**", sent[0]["system"])
        self.assertIn("Cover these 3 folders, in this order: Chapter1_Demo, Chapter2_Demo, Chapter3_Demo",
                      sent[0]["system"])
        single = {"branch": "main", "files": [window(1, "Chapter1_Demo", 600)], "citations": [], "thoughts": []}
        post, sent = groq(lambda system, user: "It parses input [S1].")
        self.explain(post, single, "high")
        self.assertIn("**History & contributors**", sent[0]["system"])

    def test_budget_wait_is_bounded_by_the_profile(self):
        self.assertTrue(llm._reserve_tokens(900, 1000))
        self.assertFalse(llm._reserve_tokens(900, 1000, max_wait=0))


if __name__ == "__main__":
    unittest.main()
