import asyncio

import httpx

from agents.auditor import Auditor, audit_from_facts
from agents.scanner import Scanner
from bus import Bus
from config import DeskSettings, defaults
from db import Database

MINT = "FixtureDevMint11111111111111111111111111"
DEV = "FixtureDevWallet111111111111111111111111"


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    return db, settings, Bus()


def _facts(**overrides):
    facts = {
        "mint": MINT,
        "ts": "2026-11-11T00:00:00+00:00",
        "settings": defaults(),
        "supply_ui": 1_000_000,
        "creator": DEV,
        "creator_balance": 1000,
        "creator_balance_at_migration": 1000,
        "mint_authority": None,
        "freeze_authority": None,
    }
    facts.update(overrides)
    return facts


def _mint_body():
    return {
        "result": {
            "value": {
                "data": {
                    "parsed": {
                        "info": {
                            "mintAuthority": None,
                            "freezeAuthority": None,
                            "supply": "1000000",
                            "decimals": 0,
                        }
                    }
                }
            }
        }
    }


def _owner_body(amount):
    return {
        "result": {
            "value": [
                {
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": MINT,
                                    "owner": DEV,
                                    "tokenAmount": {"uiAmount": amount},
                                }
                            }
                        }
                    }
                }
            ]
        }
    }


def test_missing_dev_data_fails_closed_and_names_the_cause():
    absent = audit_from_facts(_facts(creator=None, creator_balance=None, creator_balance_at_migration=None))
    assert absent["passed"] is False
    assert absent["reason"] == "no data: dev (creator absent)"
    assert absent["layers"][2]["pass"] is False
    assert absent["layers"][3]["value"] == "not checked"
    limited = audit_from_facts(
        _facts(creator_balance=None, dev_error="rpc 429")
    )
    assert limited["passed"] is False
    assert limited["reason"] == "no data: dev (rpc 429)"
    assert limited["layers"][2]["source"] == "rpc"
    sold = audit_from_facts(_facts(creator_balance=100, creator_balance_at_migration=1000))
    assert sold["reason"] == "dev sold"
    heavy = audit_from_facts(_facts(creator_balance=60_000, creator_balance_at_migration=60_000))
    assert heavy["reason"] == "dev holds 6.0%"


def test_owner_balance_uses_rpc_then_rugcheck_without_rewriting_the_baseline(tmp_path, monkeypatch):
    db, settings, bus = _desk(tmp_path)
    try:
        db.upsert_coin(mint=MINT, creator=DEV, creator_balance_at_migration=1000)
        calls = {"owner": 0}

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, timeout=None):
                method = (json or {}).get("method")
                calls["owner"] += 1 if method == "getTokenAccountsByOwner" else 0
                if method == "getTokenAccountsByOwner" and calls["owner"] == 1:
                    return httpx.Response(429, json={"message": "Too many requests"})
                if method == "getTokenAccountsByOwner":
                    return httpx.Response(200, json=_owner_body(1000))
                return httpx.Response(200, json=_mint_body())

        async def report(_mint, _client):
            return {"creator": DEV, "creatorBalance": 1000, "markets": []}

        async def graph(_mint, _client):
            return {"nodes": [], "links": []}

        async def fake_rpc(_url, method, _params, _client):
            if method == "getAccountInfo":
                return _mint_body()
            return None

        monkeypatch.setattr("agents.auditor.httpx.AsyncClient", Client)
        monkeypatch.setattr("agents.auditor.rug_report", report)
        monkeypatch.setattr("agents.auditor.rug_graph", graph)
        monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
        async def no_sleep(*_args, **_kwargs):
            return None

        monkeypatch.setattr("agents.auditor.asyncio.sleep", no_sleep)
        auditor = Auditor(db, settings, bus)
        settings.set("auditor.retries", "2", "test")

        async def run():
            return await auditor.gather({"mint": MINT, "pool": None, "liquidityUsd": 80_000, "dex": {}})

        facts = asyncio.run(run())
        assert facts["creator"] == DEV
        assert facts["creator_balance"] == 1000
        assert facts["dev_error"] is None
        assert facts["creator_balance_at_migration"] == 1000
        assert db.coin(MINT)["creator_balance_at_migration"] == 1000
        assert calls["owner"] == 2
        result = audit_from_facts(facts)
        assert result["layers"][2]["pass"] is True
        assert "sold 0" in result["layers"][2]["value"]
    finally:
        db.close()


def test_rpc_429_falls_back_to_rugcheck_current_balance(tmp_path, monkeypatch):
    db, settings, bus = _desk(tmp_path)
    try:
        db.upsert_coin(mint=MINT, creator=DEV, creator_balance_at_migration=1000)

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, timeout=None):
                return httpx.Response(429, json={"message": "Too many requests"})

        async def report(_mint, _client):
            return {"creator": DEV, "creatorBalance": 100, "markets": []}

        async def graph(_mint, _client):
            return None

        async def fake_rpc(*_args):
            return _mint_body()

        monkeypatch.setattr("agents.auditor.httpx.AsyncClient", Client)
        monkeypatch.setattr("agents.auditor.rug_report", report)
        monkeypatch.setattr("agents.auditor.rug_graph", graph)
        monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
        auditor = Auditor(db, settings, bus)

        async def run():
            return await auditor.handle({"mint": MINT, "name": "Fixture", "pool": None, "liquidityUsd": 80_000, "dex": {}})

        result = asyncio.run(run())
        assert result["passed"] is False
        assert result["reason"] == "dev sold"
        assert db.coin(MINT)["creator_balance_at_migration"] == 1000
        layer4 = next(layer for layer in result["layers"] if layer["n"] == 4)
        assert layer4["value"] == "not checked"
    finally:
        db.close()


def test_missing_migration_balance_is_not_filled_from_the_current_report(tmp_path, monkeypatch):
    db, settings, bus = _desk(tmp_path)
    try:
        db.upsert_coin(mint=MINT)

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, timeout=None):
                raise AssertionError("rpc should not run without a baseline")

        async def report(_mint, _client):
            return {"creator": DEV, "creatorBalance": 50, "markets": []}

        async def graph(_mint, _client):
            return None

        async def fake_rpc(*_args):
            return _mint_body()

        monkeypatch.setattr("agents.auditor.httpx.AsyncClient", Client)
        monkeypatch.setattr("agents.auditor.rug_report", report)
        monkeypatch.setattr("agents.auditor.rug_graph", graph)
        monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
        auditor = Auditor(db, settings, bus)
        facts = asyncio.run(auditor.gather({"mint": MINT, "pool": None, "dex": {}}))
        assert facts["dev_error"] == "migration balance missing"
        assert facts["creator_balance"] is None
        assert db.coin(MINT)["creator_balance_at_migration"] is None
        result = audit_from_facts(facts)
        assert result["reason"] == "no data: dev (migration balance missing)"
        assert result["passed"] is False
    finally:
        db.close()


def test_scanner_snapshots_dev_balance_once(tmp_path, monkeypatch):
    db, settings, bus = _desk(tmp_path)
    try:
        seen = {"n": 0}

        async def report(_mint, _client):
            seen["n"] += 1
            return {"creator": DEV, "creatorBalance": 1234}

        monkeypatch.setattr("sources.rug_report", report)
        scanner = Scanner(db, settings, bus)

        async def run():
            scanner.admit({"mint": MINT, "pool_label": "pump-amm", "migratedAt": "2026-11-11T00:00:00+00:00"})
            await asyncio.sleep(0)
            await scanner._snapshot_dev(MINT)

        asyncio.run(run())
        row = db.coin(MINT)
        assert row["creator"] == DEV
        assert row["creator_balance_at_migration"] == 1234
        assert seen["n"] == 1
    finally:
        db.close()


def test_auditor_heartbeats_while_the_queue_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.auditor.HEARTBEAT_S", 0.05)
    db, settings, bus = _desk(tmp_path)
    try:
        auditor = Auditor(db, settings, bus)

        async def run():
            task = asyncio.create_task(auditor.run())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        assert "auditor" in db.latest_heartbeats()
    finally:
        db.close()
