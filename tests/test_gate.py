from agents.analyst import activity_row, gate, rules_scores
from agents.auditor import audit_from_facts
from config import defaults
from fields import MISSING


def _settings():
    return dict(defaults())


def test_each_miss_rejects_and_threshold_passes():
    base = dict(alive=0.70, organic=0.65, flush=1.0, chase=0.30)
    assert gate(0.69, base["organic"], base["flush"], base["chase"])["passed"] is False
    assert gate(base["alive"], 0.64, base["flush"], base["chase"])["passed"] is False
    assert gate(base["alive"], base["organic"], base["flush"], 0.31)["passed"] is False
    score_case = gate(0.70, 0.65, 0.71, 0.30)
    assert score_case["score"] == 69
    assert score_case["passed"] is False
    assert score_case["failed"][0]["rule"] == "score"
    passed = gate(0.70, 0.65, 0.75, 0.30)
    assert passed["score"] == 70
    assert passed["passed"] is True
    assert passed["failed"] == []


def test_rules_mode_uses_the_same_gate():
    settings = _settings()
    low = rules_scores(
        {
            "volumeH1": 0.69 * 25000,
            "txnsH1Buys": 150,
            "txnsM5Buys": 10,
            "txnsM5Sells": 4,
            "priceChangeM5": -6,
            "priceChangeH1": 0,
        },
        0,
        settings,
    )
    assert low["passed"] is False
    assert low["failed"][0]["rule"] == "alive_6h"
    assert "Analyst (rules)" in activity_row("Coin", low, "rules")

    organic = rules_scores(
        {
            "volumeH1": 25000,
            "txnsH1Buys": 0.64 * 150,
            "txnsM5Buys": 10,
            "txnsM5Sells": 4,
            "priceChangeM5": -6,
            "priceChangeH1": 0,
        },
        0,
        settings,
    )
    assert organic["passed"] is False
    assert "Analyst (rules)" in activity_row("Coin", organic, "rules")

    chase = rules_scores(
        {
            "volumeH1": 25000,
            "txnsH1Buys": 150,
            "txnsM5Buys": 10,
            "txnsM5Sells": 4,
            "priceChangeM5": -6,
            "priceChangeH1": 0.31 * 80,
        },
        0,
        settings,
    )
    assert chase["passed"] is False
    assert chase["failed"][0]["rule"] == "chase"

    passed = rules_scores(
        {
            "volumeH1": 25000,
            "txnsH1Buys": 150,
            "txnsM5Buys": 10,
            "txnsM5Sells": 10,
            "priceChangeM5": -6,
            "priceChangeH1": 24,
        },
        0,
        settings,
    )
    assert passed["passed"] is True
    assert passed["alive_6h"] == 1
    assert passed["chase"] == 0.3


def test_freeze_authority_stops_the_audit():
    facts = {
        "mint": "Mint",
        "ts": "2026-11-06T00:00:00+00:00",
        "settings": _settings(),
        "mint_authority": None,
        "freeze_authority": "StillSet",
        "supply_ui": 1_000_000,
        "creator": "Creator",
        "creator_balance": 0,
        "creator_balance_at_migration": 0,
        "largest": [],
        "pool": "Pool",
        "curve": None,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "migrated_at": "2026-11-05T00:00:00+00:00",
        "now": None,
        "nodes": [{"id": "a", "holdings": 1}],
        "links": [],
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [],
    }
    facts["now"] = __import__("config").utc_now()
    result = audit_from_facts(facts)
    assert result["passed"] is False
    assert result["layers"][1]["n"] == 2
    assert result["layers"][1]["pass"] is False
    assert result["layers"][1]["value"] == "freeze not revoked"
    for layer in result["layers"][2:]:
        assert layer["value"] == "not checked"
        assert layer["pass"] is None
    assert MISSING is not None
