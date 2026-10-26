import base64
import binascii
import io
import re
import tarfile
from urllib.parse import quote
import httpx
from fastapi import HTTPException


SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def validate_repo_slug(owner: str, name: str) -> None:
    if not SLUG.fullmatch(owner) or not SLUG.fullmatch(name) or owner in {".", ".."} or name in {".", ".."}:
        raise HTTPException(422, "Invalid GitHub repository identifier")


class GitHubClient:
    def __init__(self, token: str | None):
        self.token = token

    def get(self, path: str, params: dict | None = None):
        try:
            with httpx.Client(timeout=15, base_url="https://api.github.com", follow_redirects=False) as client:
                response = client.get(path, params=params, headers={
                    **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "DevHive",
                })
            if response.status_code in {401, 403}:
                raise HTTPException(502, "GitHub token denied or rate limit reached" if self.token else
                                    "Public GitHub rate limit reached; connect GitHub in Settings")
            if response.status_code == 404:
                raise HTTPException(404, "GitHub resource not found")
            if response.status_code >= 400:
                raise HTTPException(502, f"GitHub returned HTTP {response.status_code}")
            return response.json()
        except httpx.RequestError as exc:
            raise HTTPException(502, "GitHub request failed") from exc
        except ValueError as exc:
            raise HTTPException(502, "Invalid GitHub response") from exc

    def write(self, method: str, path: str, body: dict):
        if not self.token:
            raise HTTPException(403, "Connect GitHub before writing")
        try:
            with httpx.Client(timeout=20, base_url="https://api.github.com", follow_redirects=False) as client:
                response = client.request(method, path, json=body, headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DevHive"})
            if response.status_code in {401, 403}:
                raise HTTPException(403, "Your GitHub account cannot write this branch")
            if response.status_code in {404, 405, 409, 422}:
                raise HTTPException(409, "GitHub rejected this write; refresh the branch or file SHA")
            if response.status_code >= 400:
                raise HTTPException(502, f"GitHub returned HTTP {response.status_code}")
            return response.json()
        except httpx.RequestError as exc:
            raise HTTPException(502, "GitHub request failed") from exc
        except ValueError as exc:
            raise HTTPException(502, "Invalid GitHub response") from exc

    def create_repository(self, name: str, description: str, private: bool):
        if not self.token:
            raise HTTPException(403, "Connect GitHub before creating a repository")
        if not SLUG.fullmatch(name) or name in {".", ".."}:
            raise HTTPException(422, "Invalid repository name")
        try:
            with httpx.Client(timeout=20, base_url="https://api.github.com", follow_redirects=False) as client:
                response = client.post("/user/repos", json={"name": name, "description": description,
                    "private": private}, headers={"Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "DevHive"})
            if response.status_code == 422:
                raise HTTPException(409, "GitHub repository name is unavailable or invalid")
            if response.status_code in {401, 403}:
                raise HTTPException(502, "GitHub token cannot create a repository")
            if response.status_code >= 400:
                raise HTTPException(502, f"GitHub returned HTTP {response.status_code}")
            return response.json()
        except httpx.RequestError as exc:
            raise HTTPException(502, "GitHub request failed") from exc

    def identity(self):
        return self.get("/user")

    def my_repositories(self):
        rows = []
        for page in range(1, 6):
            batch = self.get("/user/repos", {"visibility": "all",
                "affiliation": "owner,collaborator,organization_member", "per_page": 100, "page": page})
            rows.extend(batch)
            if len(batch) < 100:
                break
        return [{"owner": r["owner"]["login"], "name": r["name"],
                 "full_name": r["full_name"], "private": r["private"],
                 "html_url": r["html_url"], "can_push": bool(r.get("permissions", {}).get("push"))}
                for r in rows]

    def repository(self, owner: str, name: str):
        validate_repo_slug(owner, name)
        return self.get(f"/repos/{quote(owner)}/{quote(name)}")

    def branches(self, owner: str, name: str):
        validate_repo_slug(owner, name)
        rows = []
        for page in range(1, 6):
            batch = self.get(f"/repos/{quote(owner)}/{quote(name)}/branches", {"per_page": 100, "page": page})
            rows.extend(batch)
            if len(batch) < 100:
                break
            if len(batch) < 100:
                break
        return rows

    def tree(self, owner: str, name: str, sha: str):
        validate_repo_slug(owner, name)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise HTTPException(422, "Invalid tree SHA")
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/git/trees/{sha}", {"recursive": "1"})

    def create_branch(self, owner: str, name: str, source: str, target: str):
        validate_repo_slug(owner, name)
        if not source or not target or ".." in source + target:
            raise HTTPException(422, "Invalid branch")
        row = self.get(f"/repos/{quote(owner)}/{quote(name)}/branches/{quote(source, safe='/')}")
        sha = (row.get("commit") or {}).get("sha")
        if not sha or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise HTTPException(502, "GitHub returned an invalid source branch")
        return self.write("POST", f"/repos/{quote(owner)}/{quote(name)}/git/refs",
                          {"ref": f"refs/heads/{target}", "sha": sha})

    def pull_requests(self, owner: str, name: str):
        validate_repo_slug(owner, name)
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/pulls", {"state": "open", "per_page": 100})

    def pull_request(self, owner: str, name: str, number: int):
        validate_repo_slug(owner, name)
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/pulls/{number}")

    def create_pull_request(self, owner: str, name: str, head: str, base: str, title: str, body: str):
        validate_repo_slug(owner, name)
        return self.write("POST", f"/repos/{quote(owner)}/{quote(name)}/pulls",
                          {"head": head, "base": base, "title": title, "body": body})

    def merge_pull_request(self, owner: str, name: str, number: int):
        validate_repo_slug(owner, name)
        return self.write("PUT", f"/repos/{quote(owner)}/{quote(name)}/pulls/{number}/merge", {})

    def events(self, owner: str, name: str):
        validate_repo_slug(owner, name)
        rows = []
        for page in range(1, 4):
            batch = self.get(f"/repos/{quote(owner)}/{quote(name)}/events", {"per_page": 100, "page": page})
            rows.extend(batch)
            if len(batch) < 100:
                break
            if len(batch) < 100:
                break
        return rows

    def history(self, owner: str, name: str, branch: str, author: str | None = None):
        validate_repo_slug(owner, name)
        params = {"sha": branch, "per_page": 100}
        if author:
            params["author"] = author
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/commits", params)

    def commits(self, owner: str, name: str, branch: str, limit: int):
        validate_repo_slug(owner, name)
        rows: list = []
        page = 1
        while len(rows) < limit and page <= 5:
            # Keep per_page fixed: GitHub pages are offsets of that size.
            batch = self.get(f"/repos/{quote(owner)}/{quote(name)}/commits",
                             {"sha": branch, "per_page": 100 if limit > 30 else limit, "page": page})
            rows.extend(batch)
            if len(batch) < (100 if limit > 30 else limit):
                break
            page += 1
        return rows[:limit]

    def archive(self, owner: str, name: str, sha: str, max_bytes: int = 60_000_000,
                max_file_bytes: int = 200_000) -> dict[str, str]:
        """Download one commit as a tarball and return its UTF-8 text files by path.

        One request replaces a /contents call per file. The redirect is followed
        manually and only to GitHub's codeload host.
        """
        validate_repo_slug(owner, name)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise HTTPException(422, "Invalid commit SHA")
        headers = {**({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "DevHive"}
        try:
            with httpx.Client(timeout=60, follow_redirects=False) as client:
                response = client.get(f"https://api.github.com/repos/{quote(owner)}/{quote(name)}/tarball/{sha}",
                                      headers=headers)
                if response.status_code == 302:
                    target = httpx.URL(response.headers.get("location", ""))
                    if target.scheme != "https" or target.host != "codeload.github.com":
                        raise HTTPException(502, "GitHub archive redirect rejected")
                    # The signed codeload URL needs no credentials.
                    buffer = bytearray()
                    with client.stream("GET", str(target), headers={"User-Agent": "DevHive"}) as stream:
                        if stream.status_code != 200:
                            raise HTTPException(502, f"GitHub archive returned HTTP {stream.status_code}")
                        for chunk in stream.iter_bytes():
                            buffer.extend(chunk)
                            if len(buffer) > max_bytes:
                                raise HTTPException(413, "Repository archive is too large to index")
                    data = bytes(buffer)
                elif response.status_code in {401, 403}:
                    raise HTTPException(502, "GitHub token denied or rate limit reached")
                elif response.status_code == 404:
                    raise HTTPException(404, "GitHub resource not found")
                else:
                    raise HTTPException(502, f"GitHub returned HTTP {response.status_code}")
        except httpx.RequestError as exc:
            raise HTTPException(502, "GitHub request failed") from exc
        files: dict[str, str] = {}
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive:
                    if not member.isfile() or member.size > max_file_bytes:
                        continue
                    parts = member.name.split("/")[1:]
                    if not parts or any(p in {"", ".", ".."} for p in parts) or member.name.startswith("/"):
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    raw = handle.read(max_file_bytes + 1)
                    if b"\x00" in raw[:4096]:
                        continue
                    try:
                        files["/".join(parts)] = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        # Older sources are often Latin-1/CP1252 (author names in headers).
                        files["/".join(parts)] = raw.decode("cp1252", errors="replace")
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise HTTPException(502, "GitHub archive could not be read") from exc
        return files

    def commit(self, owner: str, name: str, sha: str):
        validate_repo_slug(owner, name)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise HTTPException(422, "Invalid commit SHA")
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/commits/{sha}")

    def compare(self, owner: str, name: str, base: str, head: str):
        validate_repo_slug(owner, name)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
            raise HTTPException(422, "Invalid commit SHA")
        return self.get(f"/repos/{quote(owner)}/{quote(name)}/compare/{base}...{quote(head, safe='')}")

    def file(self, owner: str, name: str, branch: str, path: str):
        validate_repo_slug(owner, name)
        if not path or len(path) > 500 or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
            raise HTTPException(422, "Invalid path")
        row = self.get(f"/repos/{quote(owner)}/{quote(name)}/contents/{quote(path, safe='/')}", {"ref": branch})
        if not isinstance(row, dict) or row.get("type") != "file" or row.get("encoding") != "base64":
            raise HTTPException(422, "Only small text files can be previewed")
        if row.get("size", 0) > 100_000:
            raise HTTPException(413, "File preview limit is 100 KB")
        try:
            data = base64.b64decode(row["content"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeError, binascii.Error) as exc:
            raise HTTPException(422, "File is not UTF-8 text") from exc
        if len(data.encode()) > 100_000:
            raise HTTPException(413, "File preview limit is 100 KB")
        return {"path": path, "branch": branch, "content": data, "sha": row.get("sha"),
                "github_url": row.get("html_url")}
