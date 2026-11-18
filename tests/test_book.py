"""Read-only desk book: holdings, sales, and null prices."""

import json

from api import build_book, build_state, exit_words
from config import DeskSettings, iso
from db import Database
from links import coin_url

MINT = "BurnieMint111111111111111111111111111"
OTHER = "OpenMint1111111111111111111111111111"
SECRET = "fixture-path-0123456789abcdef0123456789"


def _row(**overrides):
    base = {
        "mint": MINT,
        "opened_at": "2026-11-13T20:11:00+00:00",
        "entry_price": 0.00042,
        "size_usd": 40.0,
        "peak_price": 0.00042,
        "stop_price": 0.0003948,
        "trail_active": 0,
        "closed_at": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_usd": None,
        "fees_usd": None,
        "mode": "PAPER",
        "stop_pct": 6,
        "trail_start_pct": 20,
        "trail_pct": 12,
        "timeout_h": 3,
        "timeout_min_gain_pct": 5,
        "status": "open",
        "name": "Burnie",
    }
    base.update(overrides)
    return base


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("desk.night_start", "2026-11-13T18:00:00+00:00", "test")
    settings.set("desk.night_start_equity", "200", "test")
    settings.set("desk.mode", "PAPER", "test")
    return db, settings


def test_exit_words_stay_plain():
    assert exit_words("stop") == "Stop loss"
    assert exit_words("trail") == "Take profit"
    assert exit_words("timeout") == "Time exit"
    assert exit_words("pulled") == "Liquidity pulled"
    assert exit_words(None) is None


def test_open_holding_marks_to_market_and_a_missing_price_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", SECRET)
    monkeypatch.setenv("GH_TOKEN", "fixture-token-value")
    db, settings = _desk(tmp_path)
    try:
        db.upsert_coin(mint=OTHER, name="Held", symbol="HELD")
        db.add_position(_row(mint=OTHER, name="Held", entry_price=1, size_usd=50, stop_price=0.94, status="open"))
        db.add_price(OTHER, 2, None, "2026-11-13T21:00:00+00:00")
        book = build_book(db, settings)
        held = book["holdings"][0]
        assert held["name"] == "Held"
        assert held["symbol"] == "HELD"
        assert held["spent"] == 50
        assert held["tokens"] == 50
        assert held["price"] == 2
        assert held["value"] == 100
        assert held["pnlUsd"] == 50
        assert held["pnlPct"] == 100
        assert held["stop"] == 0.94
        assert held["takeProfit"] == 1.2
        assert held["link"] == coin_url(OTHER, "solana")
        assert book["summary"]["cash"] == 150
        assert book["summary"]["holdingsValue"] == 100
        assert book["summary"]["equity"] == 250
        assert book["summary"]["soldCount"] == 0
        assert book["sold"] == []

        db.add_position(_row(mint=MINT, entry_price=1, size_usd=10, status="open"))
        missing = build_book(db, settings)
        blank = next(row for row in missing["holdings"] if row["mint"] == MINT)
        assert blank["price"] is None
        assert blank["value"] is None
        assert blank["pnlUsd"] is None
        assert blank["pnlPct"] is None
        assert missing["summary"]["holdingsValue"] is None
        assert missing["summary"]["equity"] is None
        assert missing["summary"]["cash"] == 140
        dumped = json.dumps(missing)
        assert SECRET not in dumped
        assert "fixture-token-value" not in dumped
        assert "#d=" not in dumped
        assert "d=" not in dumped
    finally:
        db.close()


def test_burnie_quick_stop_is_a_stop_loss_with_fees(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        db.upsert_coin(mint=MINT, name="Burnie", symbol="BURN")
        db.add_position(
            _row(
                status="closed",
                closed_at="2026-11-13T20:14:00+00:00",
                exit_price=0.00039,
                exit_reason="stop",
                pnl_usd=-3.10,
                fees_usd=0.35,
            )
        )
        state = build_state(db, settings)
        book = state["book"]
        assert book["summary"]["soldCount"] == 1
        assert book["summary"]["wins"] == 0
        assert book["summary"]["losses"] == 1
        assert book["summary"]["realizedToday"] == -3.10
        assert book["summary"]["realizedTotal"] == -3.10
        sold = book["sold"][0]
        assert sold["name"] == "Burnie"
        assert sold["symbol"] == "BURN"
        assert sold["heldMin"] == 3
        assert sold["reason"] == "stop"
        assert sold["reasonText"] == "Stop loss"
        assert sold["pnlUsd"] == -3.10
        assert sold["fees"] == 0.35
        assert sold["exit"] == 0.00039
        assert sold["link"] == coin_url(MINT, "solana")
        assert state["paper"] is True
        assert state["mode"] == "PAPER"
    finally:
        db.close()


def test_sold_list_keeps_the_newest_fifty_and_the_full_count(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        for index in range(51):
            minute = f"{index:02d}"
            db.add_position(
                _row(
                    mint=f"Sold{index:02d}Mint111111111111111111111111",
                    name=f"Sold{index}",
                    opened_at=f"2026-11-13T18:{minute}:00+00:00" if index < 60 else iso(__import__('datetime').datetime.now(__import__('datetime').timezone.utc)),
                    closed_at=f"2026-11-13T19:{minute}:00+00:00",
                    status="closed",
                    exit_price=1,
                    exit_reason="timeout",
                    pnl_usd=1 if index % 2 == 0 else -1,
                    fees_usd=None,
                )
            )
        book = build_book(db, settings)
        assert book["summary"]["soldCount"] == 51
        assert len(book["sold"]) == 50
        assert book["sold"][0]["soldAt"] > book["sold"][-1]["soldAt"]
        assert book["sold"][0]["reasonText"] == "Time exit"
        assert book["sold"][0]["fees"] is None
        assert book["summary"]["wins"] == 26
        assert book["summary"]["losses"] == 25
    finally:
        db.close()


def test_pipeline_names_the_failed_audit_layer(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        db.upsert_coin(mint=MINT, name="Burnie", symbol="BURN")
        db.add_scan(
            {
                "mint": MINT,
                "ts": "2026-11-13T20:10:00+00:00",
                "liquidity_usd": 80000,
                "top10_pct": 10,
                "h1_change_pct": 4,
                "age_min": 20,
                "verdict": "pass",
                "reason": None,
            }
        )
        db.add_audit_layer(MINT, "2026-11-13T20:12:00+00:00", 7, "Bundled wallets", "no data: bundles (rpc 429)", 0, "rpc")
        db.add_verdict(
            {
                "mint": MINT,
                "ts": "2026-11-13T20:13:00+00:00",
                "alive_6h": 0,
                "organic": 0,
                "flush": 0,
                "chase": 1,
                "score": 40,
                "pass": 0,
                "reason": "too late",
                "input_tokens": 0,
            }
        )
        book = build_book(db, settings)
        row = book["pipeline"][0]
        assert row["name"] == "Burnie"
        assert row["scan"] == "pass"
        assert row["audit"] == "fail"
        assert row["failedLayer"] == "Bundled wallets"
        assert row["analyst"] == "reject"
        assert row["analystReason"] == "too late"
        assert {item["name"] for item in book["health"]} == {"Scanner", "Auditor", "Analyst", "Executor", "Risk"}
    finally:
        db.close()
