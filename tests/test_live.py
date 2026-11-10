import hashlib
import json
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from api import PLAIN_HASHES, Writer
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database
from livecrypto import decrypt_envelope, encrypt_envelope, envelope_bytes, is_live_path
from publish_live import (
    COMMIT_MESSAGE,
    _BLOB_WORKERS,
    GitHub,
    GitHubError,
    PublishError,
    git_blob_sha,
    load_live_files,
    owner_repo,
    publish,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DATA_PATH = "fixture-path-0123456789abcdef0123456789"
FIXTURE_TOKEN = "fixture-token-value"
SNAP = ROOT / "tests" / "fixtures" / "live"
WEB_LIVE = ROOT / "web" / "live"
NODE = ROOT / "tests" / "decrypt_live.cjs"


def _node(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(NODE), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_roundtrip_uses_filename_as_aad():
    raw = b'{"ok":true}'
    nonce = bytes(range(12))
    envelope = encrypt_envelope(raw, FIXTURE_DATA_PATH, "state.json", nonce=nonce)
    assert envelope["v"] == 1
    assert envelope["alg"] == "AES-256-GCM"
    assert len(envelope["n"]) >= 16
    assert decrypt_envelope(envelope, FIXTURE_DATA_PATH, "state.json") == raw
    again = encrypt_envelope(raw, FIXTURE_DATA_PATH, "state.json")
    assert again["n"] != envelope["n"]
    with pytest.raises(InvalidTag):
        decrypt_envelope(envelope, FIXTURE_DATA_PATH, "chart.json")
    with pytest.raises(InvalidTag):
        decrypt_envelope(envelope, "fixture-path-ffffffffffffffffffffffff", "state.json")
    assert not is_live_path("coin/../state.json")
    assert is_live_path("coin/Mint111.json")


def test_snapshot_envelopes_are_real_ciphertext():
    for name in ("state.json", "chart.json", "activity.json"):
        raw = (SNAP / name).read_bytes()
        assert raw == (WEB_LIVE / name).read_bytes()
        envelope = json.loads(raw)
        assert envelope["v"] == 1
        assert envelope["alg"] == "AES-256-GCM"
        assert FIXTURE_DATA_PATH not in raw.decode("ascii")
        assert b'"balance"' not in raw
        with pytest.raises(InvalidTag):
            decrypt_envelope(envelope, FIXTURE_DATA_PATH, name)
        proc = _node("snapshot", FIXTURE_DATA_PATH, name, str(SNAP / name))
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "rejected"
        assert FIXTURE_DATA_PATH not in proc.stdout
        assert FIXTURE_DATA_PATH not in proc.stderr


def test_browser_helper_decrypts_python_envelope(tmp_path):
    raw = b'{"ok":true}'
    name = "coin/Mint111.json"
    envelope = encrypt_envelope(raw, FIXTURE_DATA_PATH, name, nonce=bytes(range(12)))
    path = tmp_path / "coin.json"
    path.write_bytes(envelope_bytes(envelope))
    decrypted = _node("decrypt", FIXTURE_DATA_PATH, str(path), name)
    assert decrypted.returncode == 0, decrypted.stderr
    assert json.loads(decrypted.stdout) == {"ok": True}
    assert FIXTURE_DATA_PATH not in decrypted.stdout
    fallback = _node("fallback", FIXTURE_DATA_PATH, str(path))
    assert fallback.returncode == 0, fallback.stderr
    assert fallback.stdout == "fallback"
    assert FIXTURE_DATA_PATH not in fallback.stderr
    plain = _node("prefer-plain", FIXTURE_DATA_PATH)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "plain"
    status = _node("status", FIXTURE_DATA_PATH)
    assert status.returncode == 0, status.stderr
    assert status.stdout == "status"
    rejected = _node("reject", FIXTURE_DATA_PATH, str(SNAP / "state.json"), "state.json")
    assert rejected.returncode == 0, rejected.stderr
    assert rejected.stdout == "rejected"


def test_writer_emits_plaintext_and_envelopes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    monkeypatch.setenv("JSON_ROOT", str(tmp_path / "json"))
    monkeypatch.setenv("LIVE_ROOT", str(tmp_path / "live"))
    db = Database(tmp_path / "desk.db")
    try:
        settings = DeskSettings(db)
        mint = "ReplayMint1111111111111111111111111"
        db.add_audit_layer(mint, iso(utc_now()), 1, "mint", "ok", 1, "test")
        db.add_audit_layer("../outside", iso(utc_now()), 1, "mint", "ok", 1, "test")
        Writer(db, settings, Bus()).write_all()
        plain_root = tmp_path / "json" / "d" / FIXTURE_DATA_PATH
        live_root = tmp_path / "live"
        for name in ("state.json", "chart.json", "activity.json", f"coin/{mint}.json"):
            raw = (plain_root / name).read_bytes()
            envelope = json.loads((live_root / name).read_text(encoding="utf-8"))
            assert decrypt_envelope(envelope, FIXTURE_DATA_PATH, name) == raw
            assert FIXTURE_DATA_PATH.encode("utf-8") not in (live_root / name).read_bytes()
        assert not (tmp_path / "outside.json").exists()
        assert not (live_root / "outside.json").exists()
        assert not (plain_root / "outside.json").exists()
        state = json.loads((plain_root / "state.json").read_text(encoding="utf-8"))
        assert "balance" in state
        assert FIXTURE_DATA_PATH not in json.dumps(state)
    finally:
        db.close()


def test_writer_skips_without_a_long_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", "too-short")
    monkeypatch.setenv("JSON_ROOT", str(tmp_path / "json"))
    monkeypatch.setenv("LIVE_ROOT", str(tmp_path / "live"))
    db = Database(tmp_path / "desk.db")
    try:
        Writer(db, DeskSettings(db), Bus()).write_all()
        assert not (tmp_path / "json").exists()
        assert not (tmp_path / "live").exists()
    finally:
        db.close()


def test_writer_skips_unchanged_coin_envelopes_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    monkeypatch.setenv("JSON_ROOT", str(tmp_path / "json"))
    monkeypatch.setenv("LIVE_ROOT", str(tmp_path / "live"))
    db = Database(tmp_path / "desk.db")
    try:
        settings = DeskSettings(db)
        mint = "ReplayMint1111111111111111111111111"
        name = f"coin/{mint}.json"
        db.add_audit_layer(mint, iso(utc_now()), 1, "mint", "ok", 1, "test")
        Writer(db, settings, Bus()).write_all()
        live = tmp_path / "live" / name
        plain = tmp_path / "json" / "d" / FIXTURE_DATA_PATH / name
        first = live.read_bytes()
        stamp = live.stat().st_mtime_ns
        plain_stamp = plain.stat().st_mtime_ns
        Writer(db, settings, Bus()).write_all()
        assert live.read_bytes() == first
        assert live.stat().st_mtime_ns == stamp
        assert plain.stat().st_mtime_ns == plain_stamp
        Writer(db, settings, Bus()).write_all()
        assert live.read_bytes() == first
        assert live.stat().st_mtime_ns == stamp
        sidecar = tmp_path / "live" / PLAIN_HASHES
        text = sidecar.read_text(encoding="utf-8")
        assert FIXTURE_DATA_PATH not in text
        assert plain.read_text(encoding="utf-8") not in text
        stored = json.loads(text)
        assert stored[name] == hashlib.sha256(plain.read_bytes()).hexdigest()
        assert all(len(value) == 64 for value in stored.values())
        assert not is_live_path(PLAIN_HASHES)
        published = load_live_files(tmp_path / "live")
        assert PLAIN_HASHES not in published
        assert published[name] == first
        db.upsert_coin(mint=mint, name="Changed", symbol="CHG")
        Writer(db, settings, Bus()).write_all()
        rewritten = live.read_bytes()
        assert rewritten != first
        assert live.stat().st_mtime_ns != stamp
        assert json.loads(decrypt_envelope(json.loads(rewritten), FIXTURE_DATA_PATH, name))["name"] == "Changed"
    finally:
        db.close()


def test_blob_sha_matches_git(tmp_path):
    content = b'{"v":1}\n'
    path = tmp_path / "blob.json"
    path.write_bytes(content)
    expected = subprocess.check_output(["git", "hash-object", str(path)], text=True).strip()
    assert git_blob_sha(content) == expected


def test_owner_repo_drops_userinfo():
    url = "https://x-access-token:fixture-token-value@github.com/Coder2137/grok-desk.git"
    assert owner_repo(url) == "Coder2137/grok-desk"
    assert owner_repo("git@github.com:Coder2137/grok-desk.git") == "Coder2137/grok-desk"
    assert "fixture-token-value" not in (owner_repo(url) or "")


def test_publish_requires_the_core_files(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    (root / "chart.json").write_bytes(
        envelope_bytes(encrypt_envelope(b"{}", FIXTURE_DATA_PATH, "chart.json", nonce=bytes(range(12))))
    )
    github = _github(_Api())
    try:
        with pytest.raises(PublishError) as exc:
            publish(root, github)
        assert "incomplete" in str(exc.value)
    finally:
        github.close()


def test_load_live_files_rejects_plaintext(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    (root / "state.json").write_text('{"balance":1}', encoding="utf-8")
    with pytest.raises(PublishError):
        load_live_files(root)


class _Api:
    def __init__(self) -> None:
        self.ref = None
        self.pages_ref = None
        self.commits = {
            "mainsha": {"tree": "maintree", "parents": [], "message": "main"},
        }
        self.trees = {"maintree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": git_blob_sha(b"readme")}]}
        self.fail_pages = False
        self.bodies = []
        self.tree_posts = []
        self.blob_posts = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {FIXTURE_TOKEN}"
        assert FIXTURE_TOKEN not in str(request.url)
        if request.content:
            with self._lock:
                self.bodies.append(request.content.decode("utf-8"))
        path = request.url.path
        prefix = "/repos/example/desk"
        assert path.startswith(prefix)
        rest = path[len(prefix) :]
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and rest == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "mainsha", "type": "commit"}})
        if request.method == "GET" and rest == "/git/ref/heads/live-data":
            if self.ref is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.ref, "type": "commit"}})
        if request.method == "GET" and rest == "/git/ref/heads/pages-src":
            if self.pages_ref is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.pages_ref, "type": "commit"}})
        if request.method == "GET" and rest.startswith("/git/commits/"):
            sha = rest.rsplit("/", 1)[-1]
            commit = self.commits[sha]
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": commit["tree"]}, "message": commit["message"]})
        if request.method == "GET" and rest.startswith("/git/trees/"):
            sha = rest.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"sha": sha, "tree": self.trees[sha], "truncated": False})
        if request.method == "POST" and rest == "/git/blobs":
            assert body.get("encoding") == "utf-8"
            content = body["content"].encode("utf-8")
            with self._lock:
                self.blob_posts.append(content)
            return httpx.Response(201, json={"sha": git_blob_sha(content)})
        if request.method == "POST" and rest == "/git/trees":
            self.tree_posts.append(body)
            merged = {}
            if body.get("base_tree"):
                for item in self.trees[body["base_tree"]]:
                    merged[item["path"]] = dict(item)
            for entry in body["tree"]:
                assert "content" not in entry
                merged[entry["path"]] = {
                    "path": entry["path"],
                    "mode": "100644",
                    "type": "blob",
                    "sha": entry["sha"],
                }
            tree_sha = f"tree{len(self.trees) + 1}"
            self.trees[tree_sha] = [merged[path] for path in sorted(merged)]
            return httpx.Response(201, json={"sha": tree_sha})
        if request.method == "POST" and rest == "/git/commits":
            sha = f"commit{len(self.commits) + 1}"
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha})
        if request.method == "POST" and rest == "/git/refs":
            if body["ref"] == "refs/heads/live-data":
                self.ref = body["sha"]
            elif body["ref"] == "refs/heads/pages-src":
                if self.fail_pages:
                    return httpx.Response(403, json={"message": f"denied {FIXTURE_TOKEN}"})
                self.pages_ref = body["sha"]
            else:
                return httpx.Response(500, json={"message": "unexpected ref"})
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if request.method == "PATCH" and rest == "/git/refs/heads/live-data":
            assert body["force"] is False
            self.ref = body["sha"]
            return httpx.Response(200, json={"object": {"sha": self.ref}})
        if request.method == "PATCH" and rest == "/git/refs/heads/pages-src":
            assert body["force"] is False
            if self.fail_pages:
                return httpx.Response(403, json={"message": f"denied {FIXTURE_TOKEN}"})
            self.pages_ref = body["sha"]
            return httpx.Response(200, json={"object": {"sha": self.pages_ref}})
        return httpx.Response(500, json={"message": "unexpected"})


def _github(api: _Api) -> GitHub:
    client = httpx.Client(transport=httpx.MockTransport(api.handler), base_url="https://api.github.com")
    return GitHub(FIXTURE_TOKEN, "example/desk", client=client)


def _envelopes(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "coin").mkdir()
    samples = {
        "state.json": b'{"balance":1}',
        "chart.json": b'{"points":[]}',
        "activity.json": b'{"rows":[]}',
        "coin/Mint111.json": b'{"ok":true}',
    }
    for name, raw in samples.items():
        (root / name).write_bytes(envelope_bytes(encrypt_envelope(raw, FIXTURE_DATA_PATH, name, nonce=bytes(range(12)))))


def test_publish_creates_branch_and_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    monkeypatch.setenv("GH_TOKEN", FIXTURE_TOKEN)
    root = tmp_path / "live"
    _envelopes(root)
    api = _Api()
    github = _github(api)
    try:
        assert publish(root, github) == "updated"
        first = api.ref
        assert api.commits[first]["message"] == COMMIT_MESSAGE
        assert api.commits[first]["parents"] == []
        paths = [item["path"] for item in api.trees[api.commits[first]["tree"]]]
        assert paths == ["activity.json", "chart.json", "coin/Mint111.json", "state.json"]
        site = api.commits[api.pages_ref]
        assert site["parents"] == ["mainsha"]
        assert site["message"] == f"Publish live snapshot {first}"
        site_paths = [item["path"] for item in api.trees[site["tree"]]]
        assert site_paths == [
            "README.md",
            "web/live/activity.json",
            "web/live/chart.json",
            "web/live/coin/Mint111.json",
            "web/live/state.json",
        ]
        assert publish(root, github) == "unchanged"
        assert api.ref == first
        tree = api.trees[api.commits[first]["tree"]]
        tree.append({"path": "README.md", "mode": "100644", "type": "blob", "sha": git_blob_sha(b"hi\n")})
        assert publish(root, github) == "unchanged"
        assert api.ref == first
        first_pages = api.pages_ref
        (root / "state.json").write_bytes(
            envelope_bytes(encrypt_envelope(b'{"balance":2}', FIXTURE_DATA_PATH, "state.json", nonce=bytes(range(12))))
        )
        assert publish(root, github) == "updated"
        new_paths = [item["path"] for item in api.trees[api.commits[api.ref]["tree"]]]
        assert "README.md" in new_paths
        assert "coin/Mint111.json" in new_paths
        assert api.commits[api.ref]["parents"] == [first]
        assert api.commits[api.pages_ref]["parents"] == [first_pages]
        blob = "\n".join(api.bodies)
        assert FIXTURE_DATA_PATH not in blob
        assert FIXTURE_TOKEN not in blob
    finally:
        github.close()


def test_publish_retries_pages_src_without_reprinting_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", FIXTURE_TOKEN)
    root = tmp_path / "live"
    _envelopes(root)
    api = _Api()
    api.fail_pages = True
    github = _github(api)
    try:
        with pytest.raises(GitHubError) as exc:
            publish(root, github)
        assert FIXTURE_TOKEN not in str(exc.value)
        assert "[redacted]" in str(exc.value)
        assert (tmp_path / "live.dispatch-pending").is_file()
        live_commits = [sha for sha in api.commits if sha != "mainsha" and api.commits[sha]["parents"] == []]
        api.fail_pages = False
        assert publish(root, github) == "published"
        assert not (tmp_path / "live.dispatch-pending").is_file()
        assert api.pages_ref
        assert api.commits[api.pages_ref]["parents"] == ["mainsha"]
        assert live_commits == [api.ref]
    finally:
        github.close()


def test_publish_refuses_secret_material(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    root = tmp_path / "live"
    root.mkdir()
    payload = {"v": 1, "alg": "AES-256-GCM", "n": "AAAAAAAAAAAAAAAA", "c": FIXTURE_DATA_PATH}
    (root / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    github = _github(_Api())
    try:
        with pytest.raises(PublishError) as exc:
            publish(root, github)
        assert FIXTURE_DATA_PATH not in str(exc.value)
    finally:
        github.close()


def test_main_does_not_print_the_token(monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", FIXTURE_TOKEN)
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/desk")

    def boom(root, github):
        raise GitHubError(401, f"bad {FIXTURE_TOKEN}")

    monkeypatch.setattr("publish_live.publish", boom)
    from publish_live import main

    with pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "401" in captured.err
    assert FIXTURE_TOKEN not in captured.err
    assert FIXTURE_TOKEN not in captured.out
    assert "[redacted]" in captured.err


def test_main_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from publish_live import main

    with pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "GH_TOKEN" in captured.err
    assert FIXTURE_TOKEN not in captured.err


def test_github_client_does_not_follow_redirects():
    github = GitHub(FIXTURE_TOKEN, "example/desk")
    try:
        assert github._client.follow_redirects is False
        assert "fixture-token-value" not in repr(github)
    finally:
        github.close()


def test_pages_workflow_publishes_web_and_live_without_listing_secret_paths():
    text = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "pages-src" in text
    assert "refs/heads/main" in text
    assert "live-data" in text
    assert "web/live" in text
    assert "path: web" in text
    assert "ref: main" not in text
    assert "GITHUB_SHA" not in text
    assert "artifact_id" not in text
    assert "/d/" not in text
    assert "DATA_PATH" not in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "publish_live.py" in readme
    assert "GH_TOKEN" in readme
    assert "live-data" in readme
    assert "pages-src" in readme
    assert "pages_build_version" in readme
    assert "var/live" in readme
    assert "ghp_" not in readme
    assert "github_pat_" not in readme


def _retry_github(handler, sleeps):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    return GitHub(FIXTURE_TOKEN, "example/desk", client=client, sleep=sleeps.append)


def test_request_retries_502_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, json={"message": f"Server Error {FIXTURE_TOKEN}"})
        return httpx.Response(200, json={"ok": True})

    github = _retry_github(handler, sleeps)
    try:
        response = github.request("GET", "/repos/example/desk/git/ref/heads/main")
        assert response.status_code == 200
        assert calls["n"] == 2
        assert sleeps == [2]
        assert FIXTURE_TOKEN not in "".join(str(item) for item in sleeps)
    finally:
        github.close()


def test_request_raises_after_four_502s():
    calls = {"n": 0}
    sleeps = []

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(502, json={"message": f"Server Error {FIXTURE_TOKEN}"})

    github = _retry_github(handler, sleeps)
    try:
        with pytest.raises(GitHubError) as exc:
            github.request("POST", "/repos/example/desk/git/commits", {"message": "x"})
        assert exc.value.status == 502
        assert calls["n"] == 4
        assert sleeps == [2, 4, 8]
        assert FIXTURE_TOKEN not in str(exc.value)
    finally:
        github.close()


def test_request_does_not_retry_404():
    calls = {"n": 0}
    sleeps = []

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(404, json={"message": "Not Found"})

    github = _retry_github(handler, sleeps)
    try:
        with pytest.raises(GitHubError) as exc:
            github.request("GET", "/repos/example/desk/git/ref/heads/missing")
        assert exc.value.status == 404
        assert calls["n"] == 1
        assert sleeps == []
    finally:
        github.close()


def test_request_retries_transport_error_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection dropped")
        return httpx.Response(200, json={"ok": True})

    github = _retry_github(handler, sleeps)
    try:
        response = github.request("GET", "/repos/example/desk/git/ref/heads/main")
        assert response.status_code == 200
        assert calls["n"] == 2
        assert sleeps == [2]
    finally:
        github.close()


def test_patch_422_is_success_when_the_tip_matches():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request.method)
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Reference cannot be updated"})
        return httpx.Response(200, json={"object": {"sha": "abc"}})

    github = _retry_github(handler, sleeps)
    try:
        response = github.request(
            "PATCH",
            "/repos/example/desk/git/refs/heads/pages-src",
            {"sha": "abc", "force": False},
        )
        assert response.status_code == 200
        assert calls == ["PATCH", "GET"]
        assert sleeps == []
    finally:
        github.close()


def test_request_retries_422_at_most_once():
    sleeps = []
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(
            422,
            json={"message": f"Sorry, your request timed out. It's likely that your input was too large. {FIXTURE_TOKEN}"},
        )

    github = _retry_github(handler, sleeps)
    try:
        with pytest.raises(GitHubError) as exc:
            github.request("POST", "/repos/example/desk/git/trees", {"tree": []})
        assert exc.value.status == 422
        assert calls["n"] == 2
        assert sleeps == [2]
        assert FIXTURE_TOKEN not in str(exc.value)
    finally:
        github.close()


def test_publish_sends_only_changed_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    root = tmp_path / "live"
    _envelopes(root)
    nonce = bytes(range(12))
    for index in range(1000):
        name = f"coin/Coin{index:04d}.json"
        raw = f'{{"i":{index}}}'.encode()
        (root / name).write_bytes(envelope_bytes(encrypt_envelope(raw, FIXTURE_DATA_PATH, name, nonce=nonce)))
    api = _Api()
    github = _github(api)
    try:
        assert publish(root, github) == "updated"
        kept = root / "coin" / "Coin0000.json"
        kept.unlink()
        api.tree_posts.clear()
        api.blob_posts.clear()
        for name in ("state.json", "chart.json", "activity.json"):
            (root / name).write_bytes(
                envelope_bytes(encrypt_envelope(b'{"changed":true}', FIXTURE_DATA_PATH, name, nonce=nonce))
            )
        assert publish(root, github) == "updated"
        assert [len(body["tree"]) for body in api.tree_posts] == [3, 3]
        assert all(body.get("base_tree") for body in api.tree_posts)
        sent = sorted(entry["path"] for body in api.tree_posts for entry in body["tree"])
        assert sent == [
            "activity.json",
            "chart.json",
            "state.json",
            "web/live/activity.json",
            "web/live/chart.json",
            "web/live/state.json",
        ]
        assert len(api.blob_posts) == 3
        live_paths = [item["path"] for item in api.trees[api.commits[api.ref]["tree"]]]
        assert "coin/Coin0000.json" in live_paths
        assert sum(path.startswith("coin/") for path in live_paths) == 1001
    finally:
        github.close()


def test_changed_blobs_upload_on_a_bounded_pool():
    inflight = {"n": 0, "max": 0, "arrived": 0}
    lock = threading.Lock()
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/git/blobs")
        with lock:
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            inflight["arrived"] += 1
            if inflight["arrived"] >= _BLOB_WORKERS:
                release.set()
        assert release.wait(timeout=2)
        with lock:
            inflight["n"] -= 1
        body = json.loads(request.content)
        content = body["content"].encode("utf-8")
        return httpx.Response(201, json={"sha": git_blob_sha(content)})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    github = GitHub(FIXTURE_TOKEN, "example/desk", client=client)
    try:
        files = {f"coin/M{index:02d}.json": f'{{"i":{index}}}'.encode() for index in range(16)}
        unchanged = files["coin/M00.json"]
        remote = {"coin/M00.json": git_blob_sha(unchanged)}
        entries = github._changed_entries(files, remote)
        assert len(entries) == 15
        assert [item["path"] for item in entries] == sorted(path for path in files if path != "coin/M00.json")
        assert inflight["max"] == _BLOB_WORKERS
        assert {item["sha"] for item in entries} == {git_blob_sha(files[item["path"]]) for item in entries}
    finally:
        github.close()
