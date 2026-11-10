"""Publish encrypted live files.

Run this on the operator machine. It reads GH_TOKEN or GITHUB_TOKEN from the
environment and does not print that token or DATA_PATH.

live-data stays a ciphertext-only branch. pages-src starts from main, then each
later commit keeps that tree and replaces only the web/live blobs that changed.
Pushing pages-src runs Pages.
actions/deploy-pages uses that commit SHA as pages_build_version, so each
snapshot is a new Pages version. Dispatching the workflow on main is not used:
that reuses main's SHA, and GitHub Pages can report success while serving an
older artifact for that same version.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from livecrypto import is_live_path

LIVE_BRANCH = "live-data"
PAGES_BRANCH = "pages-src"
SOURCE_BRANCH = "main"
COMMIT_MESSAGE = "Update live desk files"
PAGES_MESSAGE = "Publish live snapshot"
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_ATTEMPTS = 4
_BACKOFF_S = (2, 4, 8)
# Four attempts plus the backoff stays under the 120s launcher limit.
_ATTEMPT_TIMEOUT_S = 20.0
# Blob uploads are the slow part of a catch-up. Eight at a time keeps ~1000 files under the launcher limit.
_BLOB_WORKERS = 8
_CORE = ("state.json", "chart.json", "activity.json")
_SECRET_ENV = (
    "DATA_PATH",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "TG_TOKEN",
    "APP_TOKEN",
    "TYPESAFE_API_KEY",
    "VAPID_PRIVATE_KEY",
)


class PublishError(Exception):
    pass


class GitHubError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status} {message}".strip())


def redact(text: str, token: str = "") -> str:
    out = text or ""
    if token:
        out = out.replace(token, "[redacted]")
    for name in _SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 8:
            out = out.replace(value, "[redacted]")
    return out


def owner_repo(url: str) -> str | None:
    text = (url or "").strip()
    if not text:
        return None
    if text.startswith("git@"):
        path = text.split(":", 1)[-1]
    else:
        path = urllib.parse.urlparse(text).path
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def repository_from_git() -> str:
    try:
        url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return owner_repo(url) or ""


def git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def dispatch_marker(root: Path) -> Path:
    return root.parent / f"{root.name}.dispatch-pending"


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def require_envelope(name: str, raw: bytes) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise PublishError(f"{name} is not an encrypted envelope")
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("alg") != "AES-256-GCM"
        or not isinstance(payload.get("n"), str)
        or not isinstance(payload.get("c"), str)
    ):
        raise PublishError(f"{name} is not an encrypted envelope")


def load_live_files(root: Path) -> dict[str, bytes]:
    root = Path(root)
    if not root.is_dir():
        return {}
    candidates = [root / name for name in _CORE]
    coin = root / "coin"
    if coin.is_dir() and _inside(root, coin):
        candidates.extend(sorted(path for path in coin.iterdir() if path.is_file() and path.suffix == ".json"))
    found: dict[str, bytes] = {}
    for path in candidates:
        if not path.is_file() or not _inside(root, path):
            continue
        relative = path.relative_to(root).as_posix()
        if not is_live_path(relative):
            continue
        raw = path.read_bytes()
        require_envelope(relative, raw)
        found[relative] = raw
    return found


def _reject_secret_material(files: dict[str, bytes]) -> None:
    secrets = []
    for name in _SECRET_ENV:
        value = os.environ.get(name) or ""
        if len(value) >= 8:
            secrets.append(value)
    for path, content in files.items():
        for value in secrets:
            if value in path or value.encode("utf-8") in content:
                raise PublishError("refusing to publish a live file that contains a secret")


def same_files(files: dict[str, bytes], remote: dict[str, str] | None, truncated: bool) -> bool:
    if remote is None or truncated:
        return False
    # Extra files on the branch are kept. A publish is due only when a local file is new or its blob changed.
    return all(remote.get(path) == git_blob_sha(content) for path, content in files.items())


class GitHub:
    def __init__(self, token: str, repo: str, client: httpx.Client | None = None, sleep=None) -> None:
        self.repo = repo
        self._token = token
        self._sleep = sleep or time.sleep
        self._blobs: dict[str, str] = {}
        self._blob_lock = threading.Lock()
        self._owned = client is None
        self._client = client or httpx.Client(
            base_url="https://api.github.com",
            timeout=_ATTEMPT_TIMEOUT_S,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"GitHub({self.repo})"

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self) -> "GitHub":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return redact(text, self._token)

    def _error_message(self, response: httpx.Response) -> str:
        message = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = str(payload.get("message") or "")
        except Exception:
            message = ""
        return self._redact(message)

    def _ref_already(self, path: str, sha: str) -> bool:
        ref = self.request("GET", path, allow=(404,))
        if ref.status_code != 200:
            return False
        try:
            return ref.json()["object"]["sha"] == sha
        except Exception:
            return False

    def request(self, method: str, path: str, body: dict | None = None, allow: tuple[int, ...] = ()) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "grok-desk-publish",
        }
        retried_422 = False
        for attempt in range(_ATTEMPTS):
            try:
                response = self._client.request(
                    method,
                    path,
                    json=body,
                    headers=headers,
                    timeout=_ATTEMPT_TIMEOUT_S,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt + 1 >= _ATTEMPTS:
                    raise GitHubError(0, self._redact(exc.__class__.__name__)) from None
                self._sleep(_BACKOFF_S[attempt])
                continue
            if response.status_code in _RETRY_STATUSES and response.status_code not in allow:
                if attempt + 1 >= _ATTEMPTS:
                    raise GitHubError(response.status_code, self._error_message(response))
                self._sleep(_BACKOFF_S[attempt])
                continue
            if (
                response.status_code == 422
                and method == "PATCH"
                and "/git/refs/" in path
                and body
                and body.get("sha")
                and self._ref_already(path, str(body["sha"]))
            ):
                return httpx.Response(200, json={"object": {"sha": body["sha"]}})
            # A too-large tree times out as 422. One retry covers a transient timeout; a second 422 is final.
            if response.status_code == 422 and response.status_code not in allow:
                if retried_422 or attempt + 1 >= _ATTEMPTS:
                    raise GitHubError(response.status_code, self._error_message(response))
                retried_422 = True
                self._sleep(_BACKOFF_S[attempt])
                continue
            if response.status_code >= 400 and response.status_code not in allow:
                raise GitHubError(response.status_code, self._error_message(response))
            return response
        raise GitHubError(0, "retry exhausted")

    def branch_state(self, branch: str) -> tuple[dict[str, str] | None, bool]:
        ref = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow=(404,))
        if ref.status_code == 404:
            return None, False
        sha = ref.json()["object"]["sha"]
        commit = self.request("GET", f"/repos/{self.repo}/git/commits/{sha}").json()
        return self._read_tree(commit["tree"]["sha"])

    def branch_tip(self, branch: str) -> str | None:
        ref = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow=(404,))
        if ref.status_code == 404:
            return None
        return ref.json()["object"]["sha"]

    def create_blob(self, content: bytes) -> str:
        digest = git_blob_sha(content)
        with self._blob_lock:
            cached = self._blobs.get(digest)
        if cached:
            return cached
        created = self.request(
            "POST",
            f"/repos/{self.repo}/git/blobs",
            {"content": content.decode("utf-8"), "encoding": "utf-8"},
        ).json()
        sha = str(created["sha"])
        with self._blob_lock:
            self._blobs[digest] = sha
        return sha

    def _tree_blobs(self, tree_sha: str) -> dict[str, str]:
        files, _truncated = self._read_tree(tree_sha)
        return files

    def _read_tree(self, tree_sha: str) -> tuple[dict[str, str], bool]:
        tree = self.request("GET", f"/repos/{self.repo}/git/trees/{tree_sha}?recursive=1").json()
        files = {}
        for item in tree.get("tree") or []:
            if item.get("type") == "blob" and item.get("path"):
                files[item["path"]] = item["sha"]
        return files, bool(tree.get("truncated"))

    def _changed_entries(self, files: dict[str, bytes], remote: dict[str, str], prefix: str = "") -> list[dict]:
        pending = []
        for path in sorted(files):
            full = f"{prefix}{path}"
            if remote.get(full) == git_blob_sha(files[path]):
                continue
            pending.append((full, files[path]))
        if not pending:
            return []

        def upload(item: tuple[str, bytes]) -> dict:
            full, content = item
            return {
                "path": full,
                "mode": "100644",
                "type": "blob",
                "sha": self.create_blob(content),
            }

        with ThreadPoolExecutor(max_workers=_BLOB_WORKERS) as pool:
            return list(pool.map(upload, pending))

    def _commit_tree(self, branch: str, message: str, files: dict[str, bytes], prefix: str = "", base: str | None = None) -> str:
        """Commit `files` onto `branch`, inheriting unchanged paths from its tip.

        `base` is the commit to start from when `branch` does not exist yet.
        Changed files are uploaded one blob at a time and referenced by sha.
        Paths missing locally are left in the tree.
        """
        ref = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow=(404,))
        parents: list[str] = []
        base_tree = None
        remote: dict[str, str] = {}
        if ref.status_code != 404:
            tip = ref.json()["object"]["sha"]
            parents = [tip]
            current = self.request("GET", f"/repos/{self.repo}/git/commits/{tip}").json()
            base_tree = current["tree"]["sha"]
            remote = self._tree_blobs(base_tree)
        elif base:
            parents = [base]
            current = self.request("GET", f"/repos/{self.repo}/git/commits/{base}").json()
            base_tree = current["tree"]["sha"]
            remote = self._tree_blobs(base_tree)
        entries = self._changed_entries(files, remote, prefix)
        if ref.status_code != 404 and not entries:
            return parents[0]
        body: dict = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        tree_sha = self.request("POST", f"/repos/{self.repo}/git/trees", body).json()["sha"]
        commit = self.request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            {"message": message, "tree": tree_sha, "parents": parents},
        ).json()["sha"]
        if ref.status_code != 404:
            self.request(
                "PATCH",
                f"/repos/{self.repo}/git/refs/heads/{branch}",
                {"sha": commit, "force": False},
            )
        else:
            self.request(
                "POST",
                f"/repos/{self.repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit},
            )
        return commit

    def commit_files(self, branch: str, files: dict[str, bytes], message: str) -> str:
        return self._commit_tree(branch, message, files)

    def commit_site(self, files: dict[str, bytes], live_sha: str) -> str:
        """Fast-forward pages-src, or start it from main.

        The tree is the branch tip (main, the first time) plus web/live blobs
        that differ. Unchanged coin files stay inherited and are not uploaded.
        No workflow dispatch.
        """
        base = None
        if self.branch_tip(PAGES_BRANCH) is None:
            base = self.branch_tip(SOURCE_BRANCH)
            if not base:
                raise PublishError("main branch was not found")
        return self._commit_tree(
            PAGES_BRANCH,
            f"{PAGES_MESSAGE} {live_sha}",
            files,
            prefix="web/live/",
            base=base,
        )


def publish(root: Path, github: GitHub) -> str:
    files = load_live_files(root)
    if not files:
        raise PublishError("no live files to publish")
    if any(name not in files for name in _CORE):
        raise PublishError("live snapshot is incomplete")
    _reject_secret_material(files)
    remote, truncated = github.branch_state(LIVE_BRANCH)
    changed = not same_files(files, remote, truncated)
    marker = dispatch_marker(root)
    if not changed and not marker.is_file():
        return "unchanged"
    if changed:
        live_sha = github.commit_files(LIVE_BRANCH, files, COMMIT_MESSAGE)
    else:
        live_sha = github.branch_tip(LIVE_BRANCH)
        if not live_sha:
            raise PublishError("live branch was not found")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")
    github.commit_site(files, live_sha)
    marker.unlink(missing_ok=True)
    return "updated" if changed else "published"


def main() -> None:
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        print("Set GH_TOKEN or GITHUB_TOKEN in the environment.", file=sys.stderr)
        raise SystemExit(1)
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip() or repository_from_git()
    if repo.count("/") != 1 or not all(repo.split("/")):
        print("Set GITHUB_REPOSITORY to owner/name.", file=sys.stderr)
        raise SystemExit(1)
    root = Path(os.environ.get("LIVE_ROOT") or "var/live")
    try:
        with GitHub(token, repo) as github:
            result = publish(root, github)
    except GitHubError as exc:
        print(redact(f"publish failed ({exc.status}) {exc.message}", token), file=sys.stderr)
        raise SystemExit(1) from None
    except PublishError as exc:
        print(redact(str(exc), token), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(redact(f"publish failed ({exc.__class__.__name__})", token), file=sys.stderr)
        raise SystemExit(1) from None
    print(result)


if __name__ == "__main__":
    main()
