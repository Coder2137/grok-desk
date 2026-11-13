import asyncio
import os
import time
from datetime import timedelta

import httpx

import auditcache
from agents.auditor import (
    MAX_AUDIT_TRIES,
    RETRY_DELAYS_S,
    Auditor,
    is_transient_reason,
)
from api import build_coin
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database
from sources import FALLBACK_RPC, PUBLIC_RPC

MINT = "C" * 32
POOL = "D" * 32
TOKEN = "fixture-token-value"


def _desk(tmp_path, migrated):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    settings.set("scanner.max_wait_min", "60", "test")
    db.upsert_coin(mint=MINT, name="Retry", migrated_at=iso(migrated), pool=POOL)
    return db, settings


def _facts(settings, now, cause):
    return {
        "mint": MINT,
        "ts": iso(now),
        "settings": settings.all(),
        "pool": POOL,
        "curve": None,
        "migrated_at": iso(now - timedelta(minutes=20)),
        "now": now,
        "creator": "E" * 32,
        "creator_balance": 1000,
        "creator_balance_at_migration": 1000,
        "supply_ui": 1_000_000,
        "mint_authority": None,
        "freeze_authority": None,
        "top10_pct": 10,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "bundle_size": None,
        "bundle_error": cause,
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 10, "delegate": None, "owner": "a", "mint": MINT, "read": True}],
    }


def test_transient_reasons_are_only_rate_limits_timeouts_and_an_unfinished_window():
    assert is_transient_reason("no data: bundles (rpc 429)")
    assert is_transient_reason("no data: bundles (early window incomplete)")
    assert is_transient_reason("no data: delegations (ReadTimeout)")
    assert not is_transient_reason("no data: bundles (links missing)")
    assert not is_transient_reason("5 bundled wallets")
    assert not is_transient_reason("pool too young")


def test_a_rate_limit_is_requeued_and_the_last_try_stays_fail_closed(tmp_path, monkeypatch):
    start = utc_now().replace(microsecond=0)
    clock = {"now": start}
    monkeypatch.setattr("agents.auditor.utc_now", lambda: clock["now"])
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_PATH", "fixture-path-0123456789abcdef0123456789")
    db, settings = _desk(tmp_path, start - timedelta(minutes=20))
    bus = Bus()
    auditor = Auditor(db, settings, bus)

    async def gather(_message):
        return _facts(settings, clock["now"], "rpc 429")

    auditor.gather = gather
    message = {"mint": MINT, "name": "Retry", "pool": POOL, "GH_TOKEN": TOKEN}

    async def first():
        return await auditor.handle(message)

    result = asyncio.run(first())
    assert result["passed"] is False
    assert result["reason"] == "no data: bundles (rpc 429)"
    assert result["retrying"] is True
    assert result["retry_in_s"] == RETRY_DELAYS_S[0]
    assert bus.analyst.empty()
    layer = next(row for row in db.audits_for(MINT) if row["layer"] == 7)
    assert layer["value_text"] == "retrying"
    assert layer["pass"] is None
    assert db.rejected_tonight(MINT, "1970") is False
    activity = db.latest_activity(5)[0]
    assert activity["kind"] == "retrying"
    phone = build_coin(db, settings, MINT)
    shown = next(item for item in phone["layers"] if item["n"] == 7)
    assert shown["value"] == "retrying"
    assert shown["pass"] is None
    assert phone["passedAudit"] is False
    saved = (tmp_path / "audit-retry.json").read_text(encoding="utf-8")
    assert TOKEN not in saved
    assert "fixture-path" not in saved
    resumed = Auditor(db, settings, Bus())
    resumed._load_pending()
    assert MINT in resumed._pending

    clock["now"] = start + timedelta(seconds=RETRY_DELAYS_S[0] - 1)
    auditor._release_due()
    assert bus.auditor.empty()
    clock["now"] = start + timedelta(seconds=RETRY_DELAYS_S[0])
    auditor._release_due()
    queued = bus.auditor.get_nowait()
    assert queued["audit_attempt"] == 1
    assert TOKEN not in queued

    async def second():
        return await auditor.handle(queued)

    again = asyncio.run(second())
    assert again["retrying"] is True
    assert again["retry_in_s"] == RETRY_DELAYS_S[1]

    queued["audit_attempt"] = MAX_AUDIT_TRIES - 1

    async def last():
        return await auditor.handle(queued)

    final = asyncio.run(last())
    assert final.get("retrying") is not True
    assert final["passed"] is False
    assert final["reason"] == "no data: bundles (rpc 429)"
    closed = next(row for row in db.audits_for(MINT) if row["layer"] == 7)
    assert closed["pass"] == 0
    assert closed["value_text"] == "no data: bundles (rpc 429)"
    assert db.latest_activity(5)[0]["kind"] == "rejected"
    assert bus.analyst.empty()
    db.close()


def test_a_retry_past_the_pool_window_is_closed_instead(tmp_path, monkeypatch):
    start = utc_now().replace(microsecond=0)
    monkeypatch.setattr("agents.auditor.utc_now", lambda: start)
    db, settings = _desk(tmp_path, start - timedelta(minutes=59))
    bus = Bus()
    auditor = Auditor(db, settings, bus)

    async def gather(_message):
        return _facts(settings, start, "early window incomplete")

    auditor.gather = gather

    async def run():
        return await auditor.handle({"mint": MINT, "name": "Retry", "pool": POOL})

    result = asyncio.run(run())
    assert result.get("retrying") is not True
    assert result["reason"] == "no data: bundles (early window incomplete)"
    assert MINT not in auditor._pending
    assert db.latest_activity(5)[0]["kind"] == "rejected"
    db.close()


def test_a_real_bundle_miss_is_not_retried(tmp_path, monkeypatch):
    start = utc_now().replace(microsecond=0)
    monkeypatch.setattr("agents.auditor.utc_now", lambda: start)
    db, settings = _desk(tmp_path, start - timedelta(minutes=20))
    auditor = Auditor(db, settings, Bus())

    async def gather(_message):
        facts = _facts(settings, start, "links missing")
        facts.pop("bundle_size")
        facts["links_missing"] = True
        return facts

    auditor.gather = gather

    async def run():
        return await auditor.handle({"mint": MINT, "name": "Retry"})

    result = asyncio.run(run())
    assert result["reason"] == "no data: bundles (links missing)"
    assert result.get("retrying") is not True
    assert db.latest_activity(5)[0]["kind"] == "rejected"
    db.close()


def test_layer_cache_is_shared_and_a_rate_limit_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    auditcache.store_layer(MINT, 7, {"bundle_size": 2, "bundle_error": None})
    shared = auditcache.load_layer(MINT, 7)
    assert shared["bundle_size"] == 2
    assert auditcache.load_layer(MINT, 7, now=time.time() + auditcache.LAYER_TTL_S + 5) is None
    before = {path.name for path in (tmp_path / "audit-cache").rglob("*")}
    auditcache.store_layer("../secret", 7, {"bundle_size": 1, "token": TOKEN})
    after = {path.name for path in (tmp_path / "audit-cache").rglob("*")}
    assert before == after
    text = (tmp_path / "audit-cache" / MINT / "7.json").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert auditcache.acquire(POOL) is True
    assert auditcache.acquire(POOL) is False
    lock = tmp_path / "audit-cache" / "locks" / f"{POOL}.lock"
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert auditcache.acquire(POOL, stale_s=90) is True
    auditcache.release(POOL)

    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    settings.set("desk.rpc_url", PUBLIC_RPC, "test")
    db.upsert_coin(
        mint=MINT,
        creator="E" * 32,
        creator_balance_at_migration=1000,
        migrated_at="2026-11-13T18:00:00+00:00",
        pool=POOL,
    )
    calls = {"signatures": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            method = (json or {}).get("method")
            if method == "getSignaturesForAddress":
                calls["signatures"] += 1
                return httpx.Response(429, json={"message": "Too many requests"})
            if method == "getTokenAccountsByOwner":
                info = {"mint": MINT, "owner": "E" * 32, "tokenAmount": {"uiAmount": 1000}}
                return httpx.Response(200, json={"result": {"value": [{"account": {"data": {"parsed": {"info": info}}}}]}})
            if method == "getProgramAccounts":
                info = {"mint": MINT, "owner": "Holder", "tokenAmount": {"uiAmount": 1}}
                return httpx.Response(200, json={"result": [{"account": {"data": {"parsed": {"info": info}}}}]})
            return httpx.Response(200, json={"result": []})

        async def get(self, url, timeout=None):
            return httpx.Response(404, json={})

    async def report(_mint, _client):
        return {
            "creator": "E" * 32,
            "creatorBalance": 1000,
            "insiderNetworks": None,
            "topHolders": [{"owner": "A", "pct": 4}],
            "markets": [],
        }

    async def graph(_mint, _client):
        return []

    async def fake_rpc(_url, method, _params, _client):
        if method == "getAccountInfo":
            return {"result": {"value": {"data": {"parsed": {"info": {
                "mintAuthority": None, "freezeAuthority": None, "supply": "1000000", "decimals": 0,
            }}}}}}
        return None

    monkeypatch.setattr("agents.auditor.httpx.AsyncClient", Client)
    monkeypatch.setattr("agents.auditor.rug_report", report)
    monkeypatch.setattr("agents.auditor.rug_graph", graph)
    monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
    auditcache.store_layer(MINT, 7, {"bundle_size": 2, "bundle_error": None})
    auditor = Auditor(db, settings, Bus())

    async def quiet(_seconds):
        return None

    auditor._sleep = quiet

    async def read():
        return await auditor.gather({"mint": MINT, "pool": POOL, "liquidityUsd": 80_000})

    facts = asyncio.run(read())
    assert facts["bundle_size"] == 2
    assert calls["signatures"] == 0
    other = Auditor(db, settings, Bus())
    other._sleep = quiet
    facts = asyncio.run(read_again(other))
    assert facts["bundle_size"] == 2
    assert calls["signatures"] == 0
    db.close()


async def read_again(auditor):
    return await auditor.gather({"mint": MINT, "pool": POOL, "liquidityUsd": 80_000})


def test_signature_pages_resume_and_rpc_is_paced(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    auditor = Auditor(db, settings, Bus())
    sleeps = []

    async def record(seconds):
        sleeps.append(seconds)

    auditor._sleep = record
    state = {"n": 0, "befores": []}

    class Client:
        async def post(self, url, json=None, timeout=None):
            state["n"] += 1
            before = ((json or {}).get("params") or [{}, {}])[1].get("before")
            state["befores"].append(before)
            if state["n"] == 1:
                rows = [{"signature": f"s{index}", "slot": index, "err": None} for index in range(1000)]
                return httpx.Response(200, json={"result": rows})
            return httpx.Response(429, headers={"Retry-After": "4"}, json={"message": "Too many requests"})

    async def first():
        return await auditor._signature_pages(Client(), PUBLIC_RPC, POOL, 1)

    rows, error, truncated = asyncio.run(first())
    assert rows is None
    assert error == "rpc 429"
    assert truncated is False
    assert state["befores"][0] is None
    saved = auditcache.load_pages(POOL)
    assert saved["done"] is False
    assert len(saved["signatures"]) == 1000
    assert saved["before"] == "s999"

    state["n"] = 0
    state["befores"] = []

    class Next:
        async def post(self, url, json=None, timeout=None):
            before = ((json or {}).get("params") or [{}, {}])[1].get("before")
            state["befores"].append(before)
            assert before == "s999"
            return httpx.Response(200, json={"result": [{"signature": "tail", "slot": 1, "err": None}]})

    async def second():
        return await auditor._signature_pages(Next(), PUBLIC_RPC, POOL, 1)

    rows, error, truncated = asyncio.run(second())
    assert error is None
    assert truncated is False
    assert len(rows) == 1001
    assert state["befores"] == ["s999"]

    auditor._rpc_turn = 0
    seen = []

    class Rotating:
        async def post(self, url, json=None, timeout=None):
            seen.append(str(url))
            return httpx.Response(200, json={"result": []})

    async def rotate():
        await auditor._paced_rpc(Rotating(), PUBLIC_RPC, "getSignaturesForAddress", [POOL, {}], 1)
        await auditor._paced_rpc(Rotating(), PUBLIC_RPC, "getSignaturesForAddress", [POOL, {}], 1)

    asyncio.run(rotate())
    assert PUBLIC_RPC in seen[0]
    assert FALLBACK_RPC in seen[1]

    limited = {"n": 0}

    class Limited:
        async def post(self, url, json=None, timeout=None):
            limited["n"] += 1
            if limited["n"] < 3:
                return httpx.Response(429, headers={"Retry-After": "4"}, json={"message": "Too many requests"})
            return httpx.Response(200, json={"result": []})

    sleeps.clear()

    async def backoff():
        return await auditor._paced_rpc(Limited(), PUBLIC_RPC, "getSignaturesForAddress", [POOL, {}], 2)

    body, error = asyncio.run(backoff())
    assert error is None
    assert body["result"] == []
    assert 4 in sleeps

    overlap = {"n": 0, "max": 0}

    class Slow:
        async def post(self, url, json=None, timeout=None):
            overlap["n"] += 1
            overlap["max"] = max(overlap["max"], overlap["n"])
            await asyncio.sleep(0.05)
            overlap["n"] -= 1
            return httpx.Response(200, json={"result": []})

    async def pair():
        client = Slow()
        await asyncio.gather(
            auditor._paced_rpc(client, PUBLIC_RPC, "getSignaturesForAddress", [POOL, {}], 1),
            auditor._paced_rpc(client, PUBLIC_RPC, "getSignaturesForAddress", [POOL, {}], 1),
        )

    auditor._sleep = lambda _seconds: asyncio.sleep(0)
    asyncio.run(pair())
    assert overlap["max"] == 1
    db.close()
