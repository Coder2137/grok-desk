import asyncio
import os
from pathlib import Path

import httpx

from agents.auditor import Auditor, audit_from_facts
from bus import Bus
from config import DeskSettings, defaults, utc_now
from db import Database
from fields import buyers_from_transaction, early_slot_bundle_size, insider_bundle_size, program_account_rows
from sources import TOKEN_2022, TOKEN_PROGRAM

MINT = "FixtureBundleMint11111111111111111111111"
POOL = "A" * 32
DEV = "FixtureBundleDev111111111111111111111111"


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
        "largest": None,
        "top10_pct": 10,
        "pool": "Pool",
        "curve": None,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "migrated_at": "2026-11-10T00:00:00+00:00",
        "now": utc_now(),
        "nodes": [{"id": "a", "holdings": 1}, {"id": "b", "holdings": 1}],
        "links": [{"source": "a", "target": "b"}],
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 10, "delegate": None, "owner": "a", "mint": MINT}],
    }
    facts.update(overrides)
    return facts


def _wallet(index, holdings):
    return {"id": f"Wallet{index}", "holdings": holdings}


def test_bundle_size_reads_every_network_and_the_report():
    graph = [
        {"nodes": [_wallet(0, 1)]},
        {
            "nodes": [_wallet(i, 0) for i in range(6)],
            "links": [{"source": "Wallet0", "target": "Wallet1"}],
        },
    ]
    report = {"insiderNetworks": [{"size": 12, "type": "transfer", "activeAccounts": 12}]}
    size, error = insider_bundle_size(graph, report)
    assert error is None
    assert size == 12
    size, error = insider_bundle_size(graph, {})
    assert error is None
    assert size == 6


def test_missing_graph_still_uses_the_report_size():
    size, error = insider_bundle_size(None, {"insiderNetworks": [{"size": 7, "activeAccounts": 7}]})
    assert size == 7
    assert error is None
    size, error = insider_bundle_size(None, {"creator": "Dev"})
    assert size is None
    assert error == "rugcheck unavailable"
    size, error = insider_bundle_size({"nodes": [], "links": []}, None)
    assert size == 0
    assert error is None
    size, error = insider_bundle_size([], {"insiderNetworks": None, "graphInsidersDetected": 0})
    assert size is None
    assert error == "empty network"
    size, error = insider_bundle_size([], {"insiderNetworks": [], "graphInsidersDetected": 0})
    assert size is None
    assert error == "empty network"
    size, error = insider_bundle_size([], {"insiderNetworks": None, "graphInsidersDetected": 6})
    assert size == 6
    assert error is None


def test_unmeasured_bundle_cannot_pass_and_a_real_network_still_uses_the_cap():
    missing = audit_from_facts(_facts(bundle_size=None, bundle_error="links missing"))
    assert missing["passed"] is False
    assert missing["reason"] == "no data: bundles (links missing)"
    assert missing["layers"][6]["pass"] is False
    measured_none = audit_from_facts(_facts(bundle_size=1))
    assert measured_none["layers"][6]["pass"] is True
    assert measured_none["layers"][6]["value"] == "None"
    bundled = audit_from_facts(_facts(bundle_size=7))
    assert bundled["passed"] is False
    assert bundled["reason"] == "7 bundled wallets"
    assert bundled["layers"][7]["value"] == "not checked"


def test_program_accounts_result_list_is_not_accounts_missing():
    rows = program_account_rows(
        {
            "result": [
                {
                    "pubkey": "Acct",
                    "account": {
                        "data": {
                            "parsed": {
                                "info": {
                                    "mint": MINT,
                                    "owner": "Holder",
                                    "delegate": "Delegate",
                                    "tokenAmount": {"uiAmount": 5},
                                }
                            }
                        }
                    },
                }
            ]
        }
    )
    assert rows == [{"mint": MINT, "owner": "Holder", "balance": 5.0, "delegate": "Delegate"}]
    assert program_account_rows({"result": []}) == []
    assert program_account_rows({"result": {"value": []}}) == []


def _account(owner, delegate=None):
    info = {"mint": MINT, "owner": owner, "tokenAmount": {"uiAmount": 5}}
    if delegate:
        info["delegate"] = delegate
    return {"pubkey": owner, "account": {"data": {"parsed": {"info": info}}}}


def test_token2022_accounts_are_read_after_the_classic_program_is_empty(tmp_path, monkeypatch):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    bus = Bus()
    db.upsert_coin(
        mint=MINT,
        creator=DEV,
        creator_balance_at_migration=1000,
        migrated_at="2026-11-10T00:00:00+00:00",
        pool="Pool",
    )
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            method = (json or {}).get("method")
            params = (json or {}).get("params") or []
            calls.append(method if method != "getProgramAccounts" else params[0])
            if method == "getTokenAccountsByOwner":
                return httpx.Response(
                    200,
                    json={"result": {"value": [{"account": {"data": {"parsed": {"info": {
                        "mint": MINT, "owner": DEV, "tokenAmount": {"uiAmount": 1000},
                    }}}}}]}},
                )
            if method == "getProgramAccounts" and params[0] == TOKEN_PROGRAM:
                return httpx.Response(200, json={"result": []})
            if method == "getProgramAccounts" and params[0] == TOKEN_2022:
                return httpx.Response(
                    200,
                    json={"result": [_account("One", "Shared"), _account("Two", "Shared"), _account("Three")]},
                )
            if method == "getTokenLargestAccounts":
                raise AssertionError("largest accounts are not needed once program accounts answer")
            return httpx.Response(200, json={"result": {"value": None}})

    async def report(_mint, _client):
        return {
            "creator": DEV,
            "creatorBalance": 1000,
            "insiderNetworks": [{"size": 6, "activeAccounts": 6}],
            "topHolders": [{"owner": "Pool", "pct": 40}, {"owner": "A", "pct": 4}],
            "markets": [{"pubkey": "Pool", "lp": {"lpLockedPct": 100}}],
        }

    async def graph(_mint, _client):
        return [{"nodes": [_wallet(0, 1)]}, {"nodes": [_wallet(i, 0) for i in range(6)], "links": []}]

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
    auditor = Auditor(db, settings, bus)
    facts = asyncio.run(auditor.gather({"mint": MINT, "pool": "Pool", "liquidityUsd": 80_000, "dex": {}}))
    assert facts["bundle_size"] == 6
    assert [row["delegate"] for row in facts["delegation_accounts"]] == ["Shared", "Shared", None]
    assert TOKEN_PROGRAM in calls and TOKEN_2022 in calls
    result = audit_from_facts(facts)
    assert result["reason"] == "6 bundled wallets"
    assert result["passed"] is False
    db.close()


def test_largest_accounts_supply_delegates_when_program_accounts_429(tmp_path, monkeypatch):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "2", "test")
    bus = Bus()
    db.upsert_coin(mint=MINT, creator=DEV, creator_balance_at_migration=1000, migrated_at="2026-11-10T00:00:00+00:00")
    largest_calls = {"n": 0}

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
                return httpx.Response(
                    200,
                    json={"result": {"value": [{"account": {"data": {"parsed": {"info": {
                        "mint": MINT, "owner": DEV, "tokenAmount": {"uiAmount": 1000},
                    }}}}}]}},
                )
            if method == "getProgramAccounts":
                return httpx.Response(429, json={"message": "Too many requests"})
            if method == "getTokenLargestAccounts":
                largest_calls["n"] += 1
                if largest_calls["n"] == 1:
                    return httpx.Response(429, json={"message": "Too many requests"})
                return httpx.Response(200, json={"result": {"value": [{"address": "Acct"}]}})
            if method == "getMultipleAccounts":
                return httpx.Response(
                    200,
                    json={"result": {"value": [_account("Holder", "Delegate")["account"]]}},
                )
            return httpx.Response(200, json={"result": {"value": []}})

    async def report(_mint, _client):
        return {
            "creator": DEV,
            "creatorBalance": 1000,
            "insiderNetworks": [{"size": 1, "activeAccounts": 1}],
            "topHolders": [{"owner": "A", "pct": 4}],
            "markets": [{"pubkey": "Pool", "lp": {"lpLockedPct": 100}}],
        }

    async def graph(_mint, _client):
        return []

    async def fake_rpc(*_args):
        return {"result": {"value": {"data": {"parsed": {"info": {
            "mintAuthority": None, "freezeAuthority": None, "supply": "1000000", "decimals": 0,
        }}}}}}

    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr("agents.auditor.httpx.AsyncClient", Client)
    monkeypatch.setattr("agents.auditor.rug_report", report)
    monkeypatch.setattr("agents.auditor.rug_graph", graph)
    monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
    monkeypatch.setattr("agents.auditor.asyncio.sleep", no_sleep)
    auditor = Auditor(db, settings, bus)
    facts = asyncio.run(auditor.gather({"mint": MINT, "pool": "Pool", "liquidityUsd": 80_000}))
    assert largest_calls["n"] == 2
    assert facts["delegation_accounts"][0]["delegate"] == "Delegate"
    assert facts["delegations_error"] is None
    assert settings.get("desk.layer8_down") is False
    result = audit_from_facts(facts)
    layer8 = next(layer for layer in result["layers"] if layer["n"] == 8)
    assert layer8["pass"] is True
    assert layer8["value"] == "None"
    db.close()


def _buy_tx(owner, pool=POOL, funder=None):
    instructions = []
    if funder:
        instructions.append(
            {"program": "system", "parsed": {"type": "transfer", "info": {"source": funder, "destination": owner, "lamports": 1}}}
        )
    return {
        "meta": {
            "preTokenBalances": [
                {"mint": MINT, "owner": pool, "uiTokenAmount": {"uiAmount": 100}},
                {"mint": MINT, "owner": owner, "uiTokenAmount": {"uiAmount": 0}},
            ],
            "postTokenBalances": [
                {"mint": MINT, "owner": pool, "uiTokenAmount": {"uiAmount": 90}},
                {"mint": MINT, "owner": owner, "uiTokenAmount": {"uiAmount": 10}},
            ],
        },
        "transaction": {"message": {"instructions": instructions}},
    }


def test_same_slot_buyers_are_a_bundle_and_a_missing_tx_is_not():
    assert buyers_from_transaction(_buy_tx("WalletA", POOL), MINT, POOL) == ["WalletA"]
    size, error = early_slot_bundle_size(
        [
            {"slot": 5, "buyers": ["WalletA", "WalletB"], "funder": "Funder"},
            {"slot": 5, "buyers": ["WalletC"], "funder": "Funder"},
            {"slot": 6, "buyers": []},
        ]
    )
    assert error is None
    assert size == 3
    size, error = early_slot_bundle_size([{"slot": 1, "missing": True, "error": "rpc 429"}])
    assert size is None
    assert error == "rpc 429"
    size, error = early_slot_bundle_size([{"slot": 1, "buyers": []}])
    assert size is None
    assert error == "early buys missing"


def _gather_client(handler):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            return handler((json or {}).get("method"), (json or {}).get("params") or [])

    return Client


def _arm_bundle_desk(tmp_path, monkeypatch, client):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    db.upsert_coin(
        mint=MINT,
        creator=DEV,
        creator_balance_at_migration=1000,
        migrated_at="2026-11-10T00:00:00+00:00",
        pool=POOL,
    )

    async def report(_mint, _client):
        return {
            "creator": DEV,
            "creatorBalance": 1000,
            "insiderNetworks": None,
            "graphInsidersDetected": 0,
            "topHolders": [{"owner": "A", "pct": 4}],
            "markets": [{"pubkey": POOL, "lp": {"lpLockedPct": 100}}],
        }

    async def graph(_mint, _client):
        return []

    async def fake_rpc(_url, method, _params, _client):
        if method == "getAccountInfo":
            return {"result": {"value": {"data": {"parsed": {"info": {
                "mintAuthority": None, "freezeAuthority": None, "supply": "1000000", "decimals": 0,
            }}}}}}
        return None

    monkeypatch.setattr("agents.auditor.httpx.AsyncClient", client)
    monkeypatch.setattr("agents.auditor.rug_report", report)
    monkeypatch.setattr("agents.auditor.rug_graph", graph)
    monkeypatch.setattr("agents.auditor.rpc", fake_rpc)
    return db, settings, Bus()


def test_empty_network_uses_early_buys_and_then_checks_delegations(tmp_path, monkeypatch):
    def handler(method, params):
        if method == "getTokenAccountsByOwner":
            return httpx.Response(200, json={"result": {"value": [{"account": {"data": {"parsed": {"info": {
                "mint": MINT, "owner": DEV, "tokenAmount": {"uiAmount": 1000},
            }}}}}]}})
        if method == "getProgramAccounts":
            return httpx.Response(200, json={"result": [_account("Holder")]})
        if method == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [
                {"signature": "sigA", "slot": 9, "err": None},
                {"signature": "sigB", "slot": 9, "err": None},
            ]})
        if method == "getTransaction":
            owner = "WalletA" if params[0] == "sigA" else "WalletB"
            return httpx.Response(200, json={"result": _buy_tx(owner, POOL)})
        return httpx.Response(200, json={"result": {"value": []}})

    db, settings, bus = _arm_bundle_desk(tmp_path, monkeypatch, _gather_client(handler))
    facts = asyncio.run(Auditor(db, settings, bus).gather({"mint": MINT, "pool": POOL, "liquidityUsd": 80_000}))
    assert facts["bundle_size"] == 2
    assert facts["bundle_error"] is None
    result = audit_from_facts(facts)
    layer7 = next(layer for layer in result["layers"] if layer["n"] == 7)
    layer8 = next(layer for layer in result["layers"] if layer["n"] == 8)
    assert layer7["pass"] is True
    assert layer7["value"] == "2"
    assert layer8["value"] != "not checked"
    assert layer8["pass"] is True
    assert result["passed"] is True
    db.close()


def test_empty_network_stays_closed_when_the_early_read_fails(tmp_path, monkeypatch):
    def handler(method, params):
        if method == "getTokenAccountsByOwner":
            return httpx.Response(200, json={"result": {"value": [{"account": {"data": {"parsed": {"info": {
                "mint": MINT, "owner": DEV, "tokenAmount": {"uiAmount": 1000},
            }}}}}]}})
        if method == "getProgramAccounts":
            return httpx.Response(200, json={"result": []})
        if method == "getSignaturesForAddress":
            return httpx.Response(429, json={"message": "Too many requests"})
        return httpx.Response(200, json={"result": {"value": []}})

    db, settings, bus = _arm_bundle_desk(tmp_path, monkeypatch, _gather_client(handler))
    facts = asyncio.run(Auditor(db, settings, bus).gather({"mint": MINT, "pool": POOL, "liquidityUsd": 80_000}))
    assert facts["bundle_size"] is None
    assert facts["bundle_error"] == "rpc 429"
    result = audit_from_facts(facts)
    assert result["passed"] is False
    assert result["reason"] == "no data: bundles (rpc 429)"
    layer8 = next(layer for layer in result["layers"] if layer["n"] == 8)
    assert layer8["value"] == "not checked"
    assert layer8["pass"] is None
    db.close()


def test_fixture_audits_do_not_append_the_repo_log(tmp_path, monkeypatch):
    repo_log = Path("logs") / "auditor.log"
    before = repo_log.read_text(encoding="utf-8") if repo_log.exists() else ""
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    bus = Bus()
    auditor = Auditor(db, settings, bus)

    async def gather(_message):
        return _facts()

    auditor.gather = gather
    asyncio.run(auditor.handle({"mint": MINT, "name": "Fixture"}))
    after = repo_log.read_text(encoding="utf-8") if repo_log.exists() else ""
    assert after == before
    written = Path(os.environ["LOG_DIR"]) / "auditor.log"
    assert written.is_file()
    assert MINT in written.read_text(encoding="utf-8")
    assert "FixturePassMint" not in written.read_text(encoding="utf-8")
    db.close()
