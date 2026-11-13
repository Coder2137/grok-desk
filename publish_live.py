"""Publish encrypted live files.

Run this on the operator machine. It reads GH_TOKEN or GITHUB_TOKEN from the
environment and does not print that token or DATA_PATH.

live-data stays a ciphertext-only branch. pages-src is a fast-forward chain.
When main moves, the new tree is main's current tree plus the current
web/live ciphertext, so site files on main reach Pages. A main tip that has
not moved keeps the previous pages-src tree and replaces only changed
web/live blobs.
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
# More than this many changed files is a catch-up: one blob POST per file hits GitHub's content rate limit.
_BLOB_LIMIT = 40
_BLOB_WORKERS = 4
# ~150 inline files per tree POST. 1068 changed files is 8 requests, then one commit and a ref update.
_TREE_CHUNK = 150
# One git directory of coin files above this is sharded by the first character.
_DIR_LIMIT = 80
_RATE_LIMIT_S = 600
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


class RateLimitError(GitHubError):
    def __init__(self, status: int, message: str, wait_s: int) -> None:
        self.wait_s = wait_s
        super().__init__(status, message)


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


def backoff_marker(root: Path) -> Path:
    return Path(root).parent / "publish-backoff-until"


def main_tip_cache(root: Path) -> Path:
    return Path(root) / ".main-tip"


def cached_main_tip(root: Path) -> str:
    try:
        return main_tip_cache(root).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def store_main_tip(root: Path, sha: str) -> None:
    path = main_tip_cache(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8")


def backoff_remaining(root: Path) -> int:
    path = backoff_marker(root)
    try:
        until = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return max(0, until - int(time.time()))


def arm_backoff(root: Path, wait_s: int) -> None:
    path = backoff_marker(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time()) + max(1, int(wait_s))), encoding="utf-8")


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


def _live_candidates(root: Path, prefix: str) -> list[Path]:
    base = root / prefix if prefix else root
    candidates = [base / name for name in _CORE]
    coin = base / "coin"
    if coin.is_dir() and _inside(root, coin):
        candidates.extend(sorted(path for path in coin.iterdir() if path.is_file() and path.suffix == ".json"))
    return candidates


def load_live_files(root: Path) -> dict[str, bytes]:
    root = Path(root)
    if not root.is_dir():
        return {}
    candidates = _live_candidates(root, "")
    candidates.extend(_live_candidates(root, "desk2"))
    candidates.extend(_live_candidates(root, "desk3"))
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


def _flat_coin_dir(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) == 2 and parts[0] == "coin" and parts[1].endswith(".json"):
        return ""
    if len(parts) == 3 and parts[0] in ("desk2", "desk3") and parts[1] == "coin" and parts[2].endswith(".json"):
        return parts[0]
    return None


def _wide_coin_dirs(files: dict[str, bytes]) -> set[str]:
    counts: dict[str, int] = {}
    for path in files:
        desk = _flat_coin_dir(path)
        if desk is None:
            continue
        counts[desk] = counts.get(desk, 0) + 1
    return {desk for desk, count in counts.items() if count > _DIR_LIMIT}


def layout_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """Move a crowded coin directory under coin/<first letter>/ so no git tree is huge.

    The ciphertext is unchanged. The phone decrypts it with the original coin/name.json path.
    """
    wide = _wide_coin_dirs(files)
    if not wide:
        return files
    laid: dict[str, bytes] = {}
    for path, content in files.items():
        desk = _flat_coin_dir(path)
        if desk not in wide:
            laid[path] = content
            continue
        name = path.rsplit("/", 1)[-1]
        letter = name[:1] or "_"
        prefix = f"{desk}/coin/{letter}" if desk else f"coin/{letter}"
        laid[f"{prefix}/{name}"] = content
    return laid


def logical_coin_path(path: str) -> str | None:
    """coin/A/Name.json and coin/Name.json both mean the coin file Name.json."""
    parts = path.split("/")
    if parts and parts[0] == "web" and len(parts) > 2 and parts[1] == "live":
        parts = parts[2:]
    if len(parts) >= 2 and parts[0] == "coin" and parts[-1].endswith(".json"):
        return f"coin/{parts[-1]}"
    if len(parts) >= 3 and parts[0] in ("desk2", "desk3") and parts[1] == "coin" and parts[-1].endswith(".json"):
        return f"{parts[0]}/coin/{parts[-1]}"
    return None


def same_files(files: dict[str, bytes], remote: dict[str, str] | None, truncated: bool) -> bool:
    if remote is None or truncated:
        return False
    # Extra files on the branch are kept. A publish is due only when a local file is new or its blob changed.
    laid = layout_files(files)
    return all(remote.get(path) == git_blob_sha(content) for path, content in laid.items())


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

    def _rate_limit_wait(self, response: httpx.Response) -> int | None:
        if response.status_code not in (403, 429):
            return None
        message = self._error_message(response).lower()
        retry_after = response.headers.get("Retry-After")
        reset = response.headers.get("x-ratelimit-reset")
        if "rate limit" not in message and not retry_after and not reset:
            return None
        if retry_after:
            try:
                return max(1, int(float(retry_after)))
            except ValueError:
                pass
        if reset:
            try:
                delta = int(float(reset) - time.time())
                if delta > 0:
                    return delta
            except ValueError:
                pass
        return _RATE_LIMIT_S

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
            wait_s = self._rate_limit_wait(response)
            if wait_s is not None and response.status_code not in allow:
                raise RateLimitError(response.status_code, self._error_message(response), wait_s)
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

    def _pending(self, files: dict[str, bytes], remote: dict[str, str], prefix: str = "") -> list[tuple[str, bytes]]:
        pending = []
        for path in sorted(files):
            full = f"{prefix}{path}"
            if remote.get(full) == git_blob_sha(files[path]):
                continue
            pending.append((full, files[path]))
        return pending

    def _changed_entries(self, files: dict[str, bytes], remote: dict[str, str], prefix: str = "") -> list[dict]:
        return self._blob_entries(self._pending(files, remote, prefix))

    def _blob_entries(self, pending: list[tuple[str, bytes]]) -> list[dict]:
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

    def _post_tree_raw(self, entries: list[dict], base_tree: str | None) -> str:
        body: dict = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self.request("POST", f"/repos/{self.repo}/git/trees", body).json()["sha"]

    def _post_tree(self, entries: list[dict], base_tree: str | None) -> str:
        try:
            return self._post_tree_raw(entries, base_tree)
        except GitHubError as exc:
            if exc.status in (422, 502) and len(entries) > 1:
                mid = len(entries) // 2
                left = self._post_tree(entries[:mid], base_tree)
                return self._post_tree(entries[mid:], left)
            raise

    def _post_tree_chunks(self, entries: list[dict], base_tree: str | None) -> str:
        """Chain tree posts so one snapshot is never a single oversized request.

        Entries are blob shas, not file bodies. Each post keeps the previous
        tree, so coin files from earlier batches stay published.
        """
        if not entries:
            return base_tree or ""
        tree_sha = base_tree
        for start in range(0, len(entries), _TREE_CHUNK):
            tree_sha = self._post_tree(entries[start : start + _TREE_CHUNK], tree_sha)
        return tree_sha or ""

    def _inline_tree(self, pending: list[tuple[str, bytes]], base_tree: str | None) -> str:
        tree_sha = base_tree
        for start in range(0, len(pending), _TREE_CHUNK):
            entries = [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": content.decode("utf-8"),
                }
                for path, content in pending[start : start + _TREE_CHUNK]
            ]
            tree_sha = self._post_tree(entries, tree_sha)
        return tree_sha or ""

    def _tip_context(self, branch: str, base: str | None) -> tuple[int, list[str], str | None, dict[str, str]]:
        ref = self.request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", allow=(404,))
        if ref.status_code != 404:
            tip = ref.json()["object"]["sha"]
            current = self.request("GET", f"/repos/{self.repo}/git/commits/{tip}").json()
            base_tree = current["tree"]["sha"]
            return ref.status_code, [tip], base_tree, self._tree_blobs(base_tree)
        if not base:
            return ref.status_code, [], None, {}
        current = self.request("GET", f"/repos/{self.repo}/git/commits/{base}").json()
        base_tree = current["tree"]["sha"]
        return ref.status_code, [base], base_tree, self._tree_blobs(base_tree)

    def _finish_commit(self, branch: str, message: str, tree_sha: str, parents: list[str], existed: bool) -> str:
        commit = self.request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            {"message": message, "tree": tree_sha, "parents": parents},
        ).json()["sha"]
        if existed:
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

    def _commit_tree(self, branch: str, message: str, files: dict[str, bytes], prefix: str = "", base: str | None = None, catch_up: bool = False) -> str:
        """Commit `files` onto `branch`, inheriting unchanged paths from its tip.

        A catch-up (more than 40 changed files) sends inline content in chunks
        of 150 and chains base_tree. Smaller updates upload blobs and reference
        them by sha. Paths missing locally are left in the tree.
        """
        status, parents, base_tree, remote = self._tip_context(branch, base)
        pending = self._pending(files, remote, prefix)
        existed = status != 404
        if existed and not pending:
            return parents[0]
        if catch_up and len(pending) > _BLOB_LIMIT:
            tree_sha = self._inline_tree(pending, base_tree)
        else:
            tree_sha = self._post_tree(self._blob_entries(pending), base_tree)
        return self._finish_commit(branch, message, tree_sha, parents, existed)

    def _build_tree(self, mapping: dict[str, str]) -> str:
        """Create nested git trees. Each request stays within _TREE_CHUNK entries."""
        root: dict = {}
        for path, sha in mapping.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = sha

        def build(node: dict) -> str:
            entries = []
            for name in sorted(node):
                value = node[name]
                if isinstance(value, dict):
                    entries.append({"path": name, "mode": "040000", "type": "tree", "sha": build(value)})
                else:
                    entries.append({"path": name, "mode": "100644", "type": "blob", "sha": value})
            return self._post_tree_chunks(entries, None)

        return build(root)

    def _mapping(self, laid: dict[str, bytes], remote: dict[str, str]) -> dict[str, str]:
        known = set(remote.values())
        local_logical = {logical_coin_path(path) or path for path in laid}
        mapping: dict[str, str] = {}
        uploads: list[tuple[str, bytes]] = []
        for path, content in laid.items():
            digest = git_blob_sha(content)
            if digest in known:
                mapping[path] = digest
            else:
                uploads.append((path, content))
        for entry in self._blob_entries(uploads):
            mapping[entry["path"]] = entry["sha"]
        for remote_path, sha in remote.items():
            logical = logical_coin_path(remote_path)
            if logical is None or logical in local_logical:
                continue
            name = remote_path.rsplit("/", 1)[-1]
            letter = name[:1] or "_"
            desk = remote_path.split("/")[0] if remote_path.startswith(("desk2/", "desk3/")) else ""
            if remote_path.startswith("web/live/"):
                rest = remote_path[len("web/live/") :]
                desk = rest.split("/")[0] if rest.startswith(("desk2/", "desk3/")) else ""
            published = f"{desk}/coin/{letter}/{name}" if desk else f"coin/{letter}/{name}"
            mapping.setdefault(published, sha)
        return mapping

    def _commit_wide(self, branch: str, message: str, files: dict[str, bytes]) -> str:
        status, parents, _base_tree, remote = self._tip_context(branch, None)
        mapping = self._mapping(layout_files(files), remote)
        tree_sha = self._build_tree(mapping)
        return self._finish_commit(branch, message, tree_sha, parents, status != 404)

    def commit_files(self, branch: str, files: dict[str, bytes], message: str) -> str:
        if _wide_coin_dirs(files):
            return self._commit_wide(branch, message, files)
        return self._commit_tree(branch, message, files, catch_up=True)

    def _live_sha_entries(self, files: dict[str, bytes]) -> list[dict]:
        """Current ciphertext blob ids under web/live. These replace any copy on main."""
        return [
            {
                "path": f"web/live/{path}",
                "mode": "100644",
                "type": "blob",
                "sha": git_blob_sha(files[path]),
            }
            for path in sorted(files)
        ]

    def commit_site(self, files: dict[str, bytes], live_sha: str, main_tip: str, rebuild_from_main: bool) -> str:
        """Fast-forward pages-src.

        When main's tip changed, the tree starts from that commit and every
        current live file is written over web/live, including desk2/. The
        parent is the previous pages-src tip, or main the first time. When
        main has not moved, only changed web/live blobs are sent. No dispatch.
        """
        pages_tip = self.branch_tip(PAGES_BRANCH)
        if _wide_coin_dirs(files):
            live_tip = self.branch_tip(LIVE_BRANCH)
            if not live_tip:
                raise PublishError("live branch was not found")
            live_tree = self.request("GET", f"/repos/{self.repo}/git/commits/{live_tip}").json()["tree"]["sha"]
            if rebuild_from_main or not pages_tip:
                if not main_tip:
                    raise PublishError("main branch was not found")
                base_tree = self.request("GET", f"/repos/{self.repo}/git/commits/{main_tip}").json()["tree"]["sha"]
                parents = [pages_tip or main_tip]
                existed = pages_tip is not None
            else:
                _status, parents, base_tree, _remote = self._tip_context(PAGES_BRANCH, None)
                existed = True
            tree_sha = self._post_tree(
                [{"path": "web/live", "mode": "040000", "type": "tree", "sha": live_tree}],
                base_tree,
            )
            return self._finish_commit(PAGES_BRANCH, f"{PAGES_MESSAGE} {live_sha}", tree_sha, parents, existed)
        if rebuild_from_main or not pages_tip:
            if not main_tip:
                raise PublishError("main branch was not found")
            main_commit = self.request("GET", f"/repos/{self.repo}/git/commits/{main_tip}").json()
            tree_sha = self._post_tree_chunks(self._live_sha_entries(files), main_commit["tree"]["sha"])
            return self._finish_commit(
                PAGES_BRANCH,
                f"{PAGES_MESSAGE} {live_sha}",
                tree_sha,
                [pages_tip or main_tip],
                pages_tip is not None,
            )
        status, parents, base_tree, remote = self._tip_context(PAGES_BRANCH, None)
        pending = self._pending(files, remote, "web/live/")
        existed = status != 404
        if existed and not pending:
            return parents[0]
        if len(pending) > _BLOB_LIMIT:
            live, _truncated = self.branch_state(LIVE_BRANCH)
            live = live or {}
            entries = [
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": live.get(path.removeprefix("web/live/")) or git_blob_sha(content),
                }
                for path, content in pending
            ]
            tree_sha = self._post_tree_chunks(entries, base_tree)
        else:
            tree_sha = self._post_tree(self._blob_entries(pending), base_tree)
        return self._finish_commit(PAGES_BRANCH, f"{PAGES_MESSAGE} {live_sha}", tree_sha, parents, existed)


def publish(root: Path, github: GitHub) -> str:
    remaining = backoff_remaining(root)
    if remaining > 0:
        return f"skipped, GitHub rate limit backoff for {remaining}s"
    backoff_marker(root).unlink(missing_ok=True)
    try:
        return _publish(root, github)
    except RateLimitError as exc:
        arm_backoff(root, exc.wait_s)
        raise


def _publish(root: Path, github: GitHub) -> str:
    files = load_live_files(root)
    if not files:
        raise PublishError("no live files to publish")
    if any(name not in files for name in _CORE):
        raise PublishError("live snapshot is incomplete")
    _reject_secret_material(files)
    remote, truncated = github.branch_state(LIVE_BRANCH)
    changed = not same_files(files, remote, truncated)
    main_tip = github.branch_tip(SOURCE_BRANCH)
    if not main_tip:
        raise PublishError("main branch was not found")
    main_moved = cached_main_tip(root) != main_tip
    marker = dispatch_marker(root)
    if not changed and not main_moved and not marker.is_file():
        return "unchanged"
    if changed:
        live_sha = github.commit_files(LIVE_BRANCH, files, COMMIT_MESSAGE)
    else:
        live_sha = github.branch_tip(LIVE_BRANCH)
        if not live_sha:
            raise PublishError("live branch was not found")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")
    github.commit_site(files, live_sha, main_tip, main_moved)
    marker.unlink(missing_ok=True)
    store_main_tip(root, main_tip)
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
    except RateLimitError as exc:
        print(
            redact(
                f"GitHub rate limit ({exc.status}). Pausing publishes for {exc.wait_s}s. {exc.message}",
                token,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
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
