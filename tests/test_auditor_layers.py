from pathlib import Path

from agents.auditor import audit_from_facts
from agents.scanner import top10_from_holders
from config import defaults
from fields import rug_lp

MINT = "FixtureLayerMint111111111111111111111111"
POOL = "FixturePool11111111111111111111111111111111"


def _base(**overrides):
    facts = {
        "mint": MINT,
        "ts": "2026-11-11T00:00:00+00:00",
        "settings": defaults(),
        "supply_ui": 1_000_000,
        "creator": "Dev",
        "creator_balance": 1000,
        "creator_balance_at_migration": 1000,
        "mint_authority": None,
        "freeze_authority": None,
        "largest": None,
        "top10_pct": None,
        "pool": POOL,
        "curve": None,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "migrated_at": "2026-11-10T00:00:00+00:00",
        "now": __import__("config").utc_now(),
        "nodes": [{"id": "a", "holdings": 1}, {"id": "b", "holdings": 1}],
        "links": [{"source": "a", "target": "b"}],
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 10, "delegate": None, "owner": "a", "mint": MINT}],
    }
    facts.update(overrides)
    return facts


def test_rugcheck_top10_is_used_and_the_threshold_still_fails_closed():
    holders = [{"owner": POOL, "address": "lp", "pct": 60}]
    holders.extend({"owner": f"W{i}", "address": f"A{i}", "pct": 2} for i in range(11))
    assert top10_from_holders(holders, POOL, None) == 20
    passed = audit_from_facts(_base(top10_pct=20))
    assert passed["layers"][3]["pass"] is True
    assert "20" in passed["layers"][3]["value"]
    rejected = audit_from_facts(_base(top10_pct=30))
    assert rejected["passed"] is False
    assert rejected["reason"] == "top 10 hold 30%"
    assert rejected["layers"][4]["value"] == "not checked"


def test_top10_failure_names_the_rpc_limit():
    result = audit_from_facts(_base(top10_error="rpc 429"))
    assert result["passed"] is False
    assert result["reason"] == "no data: top 10 (rpc 429)"
    assert result["layers"][3]["source"] == "rpc"
    assert result["layers"][4]["value"] == "not checked"


def test_later_layers_name_the_missing_source_and_still_fail():
    liquidity = audit_from_facts(_base(top10_pct=10, lp_found=False, liquidity_error="pool not in report"))
    assert liquidity["reason"] == "no data: liquidity (pool not in report)"
    amount = audit_from_facts(_base(top10_pct=10, current_liquidity=None))
    assert amount["reason"] == "no data: liquidity (amount missing)"
    age = audit_from_facts(_base(top10_pct=10, migrated_at=None))
    assert age["reason"] == "no data: pool age (migrated_at missing)"
    bundles = audit_from_facts(
        _base(top10_pct=10, links_missing=True, empty_network=True, bundle_error="rugcheck unavailable")
    )
    assert bundles["reason"] == "no data: bundles (rugcheck unavailable)"
    delegations = audit_from_facts(
        _base(top10_pct=10, delegation_accounts=None, delegations_error="rpc 429")
    )
    assert delegations["passed"] is False
    assert delegations["reason"] == "no data: delegations (rpc 429)"


def test_rug_lp_can_match_the_pool_on_a_nested_address_and_still_rejects_a_label():
    market = {"pubkey": "other", "lp": {"lpLockedPct": 100, "baseVault": POOL}}
    assert rug_lp({"markets": [market]}, POOL)["found"] is True
    assert rug_lp({"markets": [market]}, POOL)["locked"] == 100
    assert rug_lp({"markets": [{"pubkey": "other", "lp": {"lpLockedPct": 100}}]}, "pump-amm")["found"] is False


def test_startup_log_uses_the_real_instance_id():
    text = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    assert 'f"{name} up"' in text
    assert '"desk2 up"' not in text
