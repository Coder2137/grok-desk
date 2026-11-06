import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

from agents.analyst import rules_scores
from agents.auditor import audit_from_facts
from agents.executor import Executor
from agents.risk import poll_position
from bus import Bus
from config import DeskSettings, defaults, utc_now
from db import Database
from fees import paper_pnl
from links import coin_url

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "replay.json").read_text(encoding="utf-8"))


def _facts():
    settings = defaults()
    return {
        "mint": FIXTURE["mint"],
        "ts": "2026-11-06T12:00:00+00:00",
        "settings": settings,
        "mint_authority": None,
        "freeze_authority": None,
        "supply_ui": 1_000_000,
        "creator": "Creator",
        "creator_balance": 1000,
        "creator_balance_at_migration": 1000,
        "largest": [{"address": "Holder", "owner": "Holder", "balance": 10_000}],
        "pool": FIXTURE["pool"],
        "curve": None,
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "migrated_at": (utc_now() - timedelta(hours=2)).isoformat(),
        "now": utc_now(),
        "nodes": [{"id": "a", "participant": "a", "holdings": 10}],
        "links": [],
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 10, "delegate": None, "owner": "a", "mint": FIXTURE["mint"]}],
    }


def test_recorded_migration_runs_paper_without_an_order():
    audit = audit_from_facts(_facts())
    assert audit["passed"] is True
    scored = rules_scores(FIXTURE["dex"], 0, defaults())
    assert scored["passed"] is True

    async def run():
        db = Database(Path("var") / "pytest-replay.db")
        settings = DeskSettings(db)
        bus = Bus()

        async def acks():
            while True:
                event = await bus.events.get()
                ack = event.get("ack")
                if ack and not ack.done():
                    ack.set_result(True)

        task = asyncio.create_task(acks())
        executor = Executor(db, settings, bus)
        await executor.handle(
            {
                "mint": FIXTURE["mint"],
                "name": FIXTURE["name"],
                "symbol": FIXTURE["symbol"],
                "pool": FIXTURE["pool"],
                "liquidityUsd": FIXTURE["liquidityUsd"],
                "dex": FIXTURE["dex"],
                "analystPrice": FIXTURE["price"],
                "pushPrice": FIXTURE["price"],
                "score": scored["score"],
                "passed": True,
            }
        )
        task.cancel()
        return db

    db = asyncio.run(run())
    try:
        position = dict(db.open_positions()[0])
        assert position["mode"] == "PAPER"
        assert position["size_usd"] == 47.50
        assert db.signals_since("1970")[0]["link"] == coin_url(FIXTURE["mint"], "solana")
        assert poll_position(position, FIXTURE["price"] * 1.25, utc_now()) is None
        assert position["trail_active"] == 1
        exit_price = position["peak_price"] * 0.87
        assert poll_position(position, exit_price, utc_now()) == "trail"
        pnl, fees = paper_pnl(position["entry_price"], exit_price, position["size_usd"])
        assert fees > 0
        assert pnl != 0 or fees > 0
    finally:
        db.close()
        Path("var/pytest-replay.db").unlink(missing_ok=True)


def test_repo_has_no_secret_material():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "var", "logs", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()
        assert ("mlody" + "hustler") not in lowered
        assert ("ph" + "c_") not in lowered
        assert re.search(r"DATA_PATH\s*=\s*['\"][A-Za-z0-9]{8,}", text) is None
        assert re.search(r"\d{8,}:[A-Za-z0-9_-]{20,}", text) is None
        assert ("BEGIN " + "PRIVATE KEY") not in text
