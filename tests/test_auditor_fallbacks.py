import asyncio

import httpx

from agents.auditor import Auditor, audit_from_facts
from bus import Bus
from config import DeskSettings, utc_now
from db import Database
from sources import PUBLIC_RPC, dexscreener_pair

MINT = "FixtureFallbackMint11111111111111111111"
POOL = "B" * 32
DEV = "FixtureFallbackDev111111111111111111111"


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    settings.set("desk.rpc_url", PUBLIC_RPC, "test")
    db.upsert_coin(
        mint=MINT,
        creator=DEV,
        creator_balance_at_migration=1000,
        migrated_at="2026-11-10T00:00:00+00:00",
        pool=POOL,
    )
    return db, settings


def test_dexscreener_liquidity_is_used_when_the_pool_is_not_in_the_report(tmp_path, monkeypatch):
    db, settings = _desk(tmp_path)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            method = (json or {}).get("method")
            if method == "getTokenAccountsByOwner":
                info = {"mint": MINT, "owner": DEV, "tokenAmount": {"uiAmount": 1000}}
                return httpx.Response(200, json={"result": {"value": [{"account": {"data": {"parsed": {"info": info}}}}]}})
            if method == "getProgramAccounts":
                info = {"mint": MINT, "owner": "Holder", "tokenAmount": {"uiAmount": 1}}
                return httpx.Response(200, json={"result": [{"account": {"data": {"parsed": {"info": info}}}}]})
            return httpx.Response(200, json={"result": []})

        async def get(self, url, timeout=None):
            if "dex/pairs" in str(url):
                return httpx.Response(200, json={"pairs": [{"pairAddress": POOL, "liquidity": {"usd": 12500}}]})
            return httpx.Response(404, json={})

    async def report(_mint, _client):
        return {
            "creator": DEV,
            "creatorBalance": 1000,
            "markets": [],
            "insiderNetworks": [{"size": 1}],
            "topHolders": [{"owner": "A", "pct": 4}],
        }

    async def graph(_mint, _client):
        return {"nodes": [{"id": "a", "holdings": 1}], "links": []}

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
    try:
        facts = asyncio.run(Auditor(db, settings, Bus()).gather({"mint": MINT, "pool": POOL, "liquidityUsd": 80_000}))
    finally:
        db.close()
    assert facts["lp_found"] is True
    assert facts["current_liquidity"] == 12500
    assert facts["liquidity_error"] == "lock missing"
    result = audit_from_facts(facts)
    assert result["reason"] == "liquidity pulled"
    assert result["passed"] is False


def test_signatures_page_until_the_pool_start_and_a_stuck_cursor_is_incomplete(tmp_path):
    db, settings = _desk(tmp_path)
    auditor = Auditor(db, settings, Bus())

    class Client:
        def __init__(self):
            self.pages = 0

        async def post(self, url, json=None, timeout=None):
            params = (json or {}).get("params") or []
            before = (params[1] or {}).get("before") if len(params) > 1 else None
            self.pages += 1
            if self.pages < 3:
                start = 0 if not before else int(before.split("-")[1])
                rows = [{"signature": f"sig-{start + index}", "slot": start + index, "err": None} for index in range(1000)]
                return httpx.Response(200, json={"result": rows})
            return httpx.Response(200, json={"result": [{"signature": "tail", "slot": 1, "err": None}]})

    async def run(client):
        return await auditor._signature_pages(client, PUBLIC_RPC, POOL, 1)

    try:
        rows, error, truncated = asyncio.run(run(Client()))
        assert error is None
        assert truncated is False
        assert len(rows) == 2001
        stuck = {"n": 0}

        class Stuck:
            async def post(self, url, json=None, timeout=None):
                stuck["n"] += 1
                rows = [{"signature": "same", "slot": 1, "err": None} for _ in range(1000)]
                return httpx.Response(200, json={"result": rows})

        _rows, error, truncated = asyncio.run(run(Stuck()))
        assert truncated is True
        assert error is None
        assert stuck["n"] == 2
    finally:
        db.close()
