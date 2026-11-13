import asyncio

import httpx

from agents.auditor import Auditor, audit_from_facts
from bus import Bus
from config import DeskSettings, defaults, utc_now
from db import Database
from sources import FALLBACK_RPC, PUBLIC_RPC, TOKEN_2022, TOKEN_PROGRAM

MINT = "FixtureDelegateMint111111111111111111111"
DEV = "FixtureDelegateDev1111111111111111111111"


def _holder(owner, delegate=None, permanent=None):
    info = {"mint": MINT, "owner": owner, "tokenAmount": {"uiAmount": 4}}
    if delegate:
        info["delegate"] = delegate
    if permanent:
        info["extensions"] = [{"extension": "permanentDelegate", "state": {"delegate": permanent}}]
    return {"pubkey": owner, "account": {"data": {"parsed": {"info": info}}}}


def _parsed_account(owner):
    return _holder(owner)["account"]


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
        "top10_pct": 10,
        "pool": "A" * 32,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "migrated_at": "2026-11-10T00:00:00+00:00",
        "now": utc_now(),
        "bundle_size": 1,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 4, "delegate": None, "owner": "Holder", "mint": MINT, "read": True}],
    }
    facts.update(overrides)
    return facts


def _layer(result, number):
    return next(layer for layer in result["layers"] if layer["n"] == number)


def test_unread_accounts_and_a_permanent_delegate_do_not_pass():
    missing = audit_from_facts(_facts(delegation_accounts=None, delegations_error="rpc 429"))
    assert missing["reason"] == "no data: delegations (rpc 429)"
    unread = audit_from_facts(_facts(delegation_accounts=[{"read": False, "delegate": None, "balance": None}]))
    assert unread["passed"] is False
    assert unread["reason"] == "no data: delegations (account not found)"
    permanent = audit_from_facts(_facts(permanent_delegate="PermDelegate111111111111111111111111"))
    assert permanent["passed"] is False
    assert permanent["reason"].startswith("permanent delegate ")
    clean = audit_from_facts(_facts())
    assert _layer(clean, 8)["pass"] is True
    assert _layer(clean, 8)["value"] == "None"


def _client(handler):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, timeout=None):
            return handler(url, (json or {}).get("method"), (json or {}).get("params") or [])

    return Client


def _read(tmp_path, handler, retries="1"):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", retries, "test")
    settings.set("desk.rpc_url", PUBLIC_RPC, "test")
    auditor = Auditor(db, settings, Bus())

    async def run():
        async with _client(handler)() as client:
            return await auditor._delegations(PUBLIC_RPC, MINT, client, int(retries))

    try:
        return asyncio.run(run())
    finally:
        db.close()


def test_token2022_and_standard_spl_accounts_are_read(tmp_path):
    def token2022(url, method, params):
        assert PUBLIC_RPC in url
        if method == "getProgramAccounts" and params[0] == TOKEN_PROGRAM:
            return httpx.Response(200, json={"result": []})
        if method == "getProgramAccounts" and params[0] == TOKEN_2022:
            return httpx.Response(200, json={"result": [_holder("Holder")]})
        raise AssertionError(method)

    rows, error = _read(tmp_path, token2022)
    assert error is None
    assert rows[0]["read"] is True
    assert rows[0]["delegate"] is None
    assert rows[0]["permanent_delegate"] is None
    passed = audit_from_facts(_facts(delegation_accounts=rows))
    assert _layer(passed, 8)["pass"] is True

    def standard(url, method, params):
        if method == "getProgramAccounts" and params[0] == TOKEN_PROGRAM:
            return httpx.Response(200, json={"result": [_holder("SplHolder")]})
        if method == "getProgramAccounts" and params[0] == TOKEN_2022:
            return httpx.Response(200, json={"result": []})
        raise AssertionError(method)

    rows, error = _read(tmp_path, standard)
    assert error is None
    assert rows[0]["owner"] == "SplHolder"
    assert rows[0]["delegate"] is None


def test_partial_missing_account_is_not_a_pass(tmp_path):
    def handler(url, method, params):
        if method == "getProgramAccounts":
            return httpx.Response(429, json={"message": "Too many requests"})
        if method == "getTokenLargestAccounts":
            return httpx.Response(200, json={"result": {"value": [{"address": "AcctA"}, {"address": "AcctB"}]}})
        if method == "getMultipleAccounts":
            return httpx.Response(200, json={"result": {"value": [_parsed_account("Holder"), None]}})
        raise AssertionError(method)

    rows, error = _read(tmp_path, handler)
    assert rows is None
    assert error == "account not found"
    failed = audit_from_facts(_facts(delegation_accounts=None, delegations_error=error))
    assert failed["passed"] is False
    assert failed["reason"] == "no data: delegations (account not found)"
    assert _layer(failed, 8)["value"] != "not checked"


def test_rpc_failure_is_not_reported_as_a_missing_account(tmp_path):
    def handler(url, method, params):
        return httpx.Response(429, json={"message": "Too many requests"})

    rows, error = _read(tmp_path, handler)
    assert rows is None
    assert error == "rpc 429"
    failed = audit_from_facts(_facts(delegation_accounts=None, delegations_error=error))
    assert failed["reason"] == "no data: delegations (rpc 429)"


def test_a_second_rpc_is_used_when_the_first_is_limited(tmp_path):
    seen = []

    def handler(url, method, params):
        seen.append(url)
        if PUBLIC_RPC in url:
            return httpx.Response(429, json={"message": "Too many requests"})
        assert FALLBACK_RPC in url
        if method == "getProgramAccounts" and params[0] == TOKEN_PROGRAM:
            return httpx.Response(200, json={"result": []})
        if method == "getProgramAccounts" and params[0] == TOKEN_2022:
            return httpx.Response(200, json={"result": [_holder("Fallback")]})
        raise AssertionError(method)

    rows, error = _read(tmp_path, handler)
    assert error is None
    assert rows[0]["owner"] == "Fallback"
    assert any(FALLBACK_RPC in url for url in seen)
    assert any(PUBLIC_RPC in url for url in seen)
