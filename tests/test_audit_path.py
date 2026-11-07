import asyncio
import json
from datetime import timedelta

from agents.analyst import Analyst
from agents.auditor import MISSING, Auditor
from agents.executor import Executor
from agents.scanner import Scanner
from api import Writer
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database
from fields import migration_frame
from links import coin_url
from livecrypto import decrypt_envelope

FIXTURE_DATA_PATH = "fixture-path-0123456789abcdef0123456789"
KILL_MINT = "FixtureKillMint111111111111111111111111"
PASS_MINT = "FixturePassMint111111111111111111111111"
BANNED = ("?", "utm_", "ref=", "referral", "affiliate", "onelink")


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    return db, DeskSettings(db), Bus()


def _pair(mint, liquidity, h1, volume_h1=30000, buys_h1=180):
    return {
        "dexId": "pumpswap",
        "baseToken": {"address": mint, "name": "Fixture Coin", "symbol": "FIX"},
        "pairAddress": "FixturePair1111111111111111111111111111111",
        "priceUsd": "0.001",
        "liquidity": {"usd": liquidity},
        "marketCap": None,
        "volume": {"m5": 4000, "h1": volume_h1, "h24": 90000},
        "txns": {
            "m5": {"buys": 20, "sells": 10},
            "h1": {"buys": buys_h1, "sells": 40},
        },
        "priceChange": {"m5": -6, "h1": h1, "h24": 12},
    }


def _arm(scanner, mint, minutes):
    scanner.db.upsert_coin(mint=mint, name=None, symbol=None)
    scanner.waiting[mint] = {
        "mint": mint,
        "pool": None,
        "pool_label": "pump-amm",
        "migratedAt": iso(utc_now() - timedelta(minutes=minutes)),
        "creator": None,
    }
    scanner.bus.waiting.add(mint)


def _passing_facts(settings, mint):
    now = utc_now()
    return {
        "mint": mint,
        "ts": iso(now),
        "settings": settings,
        "pool": None,
        "curve": None,
        "migrated_at": iso(now - timedelta(minutes=20)),
        "now": now,
        "creator": "FixtureCreator",
        "creator_balance": 1000,
        "creator_balance_at_migration": 1000,
        "supply_ui": 1_000_000,
        "mint_authority": None,
        "freeze_authority": None,
        "largest": [{"address": "Holder", "owner": "Holder", "balance": 10_000}],
        "lp_found": True,
        "lp_locked": 100,
        "lp_burned": None,
        "current_liquidity": 80_000,
        "scanner_liquidity": 80_000,
        "nodes": [{"id": "a", "holdings": 10}, {"id": "b", "holdings": 10}],
        "links": [{"source": "a", "target": "b"}],
        "links_missing": False,
        "empty_network": False,
        "delegations_down": False,
        "delegation_accounts": [{"balance": 10, "delegate": None, "owner": "a", "mint": mint}],
    }


def test_killed_scan_writes_coin_detail_without_an_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    monkeypatch.setenv("JSON_ROOT", str(tmp_path / "json"))
    monkeypatch.setenv("LIVE_ROOT", str(tmp_path / "live"))
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)

        async def no_top10(*_args, **_kwargs):
            return None

        scanner._top10 = no_top10
        _arm(scanner, KILL_MINT, 1.57)

        async def scan():
            await scanner.poll_once(pairs=[_pair(KILL_MINT, 256.14, -98.66)])

        asyncio.run(scan())
        scan_row = db.latest_scan(KILL_MINT)
        assert scan_row["verdict"] == "kill"
        assert scan_row["reason"] == "liq $256"
        assert scan_row["liquidity_usd"] == 256.14
        assert scan_row["top10_pct"] is None
        assert scan_row["h1_change_pct"] == -98.66
        assert db.audits_for(KILL_MINT) == []
        assert db.verdicts_since("1970") == []
        assert db.signals_since("1970") == []
        assert db.open_positions() == []
        assert db.latest_activity(20) == []
        assert bus.auditor.empty()

        Writer(db, settings, bus).write_all()
        plain = tmp_path / "json" / "d" / FIXTURE_DATA_PATH / "coin" / f"{KILL_MINT}.json"
        detail = json.loads(plain.read_text(encoding="utf-8"))
        envelope = json.loads((tmp_path / "live" / "coin" / f"{KILL_MINT}.json").read_text(encoding="utf-8"))
        assert json.loads(decrypt_envelope(envelope, FIXTURE_DATA_PATH, f"coin/{KILL_MINT}.json")) == detail
        assert detail["name"] == "Fixture Coin"
        assert detail["symbol"] == "FIX"
        assert detail["scan"]["verdict"] == "kill"
        assert detail["scan"]["reason"] == "liq $256"
        assert detail["scan"]["liquidity"] == 256.14
        assert detail["scan"]["top10"] is None
        assert detail["scan"]["h1"] == -98.66
        assert detail["holders"] is None
        assert detail["layers"] == []
        assert detail["passedAudit"] is False
        assert detail["shield"] is None
        assert detail["liquidity"] == 256.14
        assert detail["marketCap"] is None
        assert detail["link"] == coin_url(KILL_MINT, "solana")
        for banned in BANNED:
            assert banned not in detail["link"]
        assert FIXTURE_DATA_PATH not in plain.read_text(encoding="utf-8")
    finally:
        db.close()


def test_accepted_scan_reaches_paper_executor_without_a_network(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)
        _arm(scanner, PASS_MINT, 20)
        scanner.waiting[PASS_MINT]["top10"] = 10

        async def run():
            await scanner.poll_once(pairs=[_pair(PASS_MINT, 80_000, 4)])
            await asyncio.sleep(0)
            message = bus.auditor.get_nowait()
            auditor = Auditor(db, settings, bus)

            async def gather(_message):
                return _passing_facts(settings.all(), PASS_MINT)

            auditor.gather = gather
            result = await auditor.handle(message)
            assert result["passed"] is True
            assert db.coin(PASS_MINT)["holders"] is not None
            onward = bus.analyst.get_nowait()
            analyst = Analyst(db, settings, bus, api_key="")
            await analyst.handle(onward)
            assert db.verdicts_since("1970")[0]["pass"] == 1
            executor = Executor(db, settings, bus)

            async def pushed(*_args):
                return True

            executor._push = pushed
            await executor.handle(bus.executor.get_nowait())

        asyncio.run(run())
        position = db.open_positions()[0]
        assert position["mode"] == "PAPER"
        assert position["mint"] == PASS_MINT
        assert position["size_usd"] == 47.50
        assert db.signals_since("1970")[0]["link"] == coin_url(PASS_MINT, "solana")
        assert settings.get("desk.mode") == "PAPER"
        assert bus.events.empty()
    finally:
        db.close()


def test_missing_freeze_creator_and_holders_fail_closed(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        db.upsert_coin(mint=PASS_MINT)
        auditor = Auditor(db, settings, bus)
        message = {"mint": PASS_MINT, "name": "Fixture Coin", "dex": {}, "liquidityUsd": 80_000}

        async def reject(facts, reason):
            async def gather(_message):
                return facts

            auditor.gather = gather
            result = await auditor.handle(message)
            assert result["passed"] is False
            assert result["reason"] == reason
            assert bus.analyst.empty()
            assert db.coin(PASS_MINT)["holders"] is None

        missing_freeze = _passing_facts(settings.all(), PASS_MINT)
        missing_freeze["freeze_authority"] = MISSING
        present_freeze = _passing_facts(settings.all(), PASS_MINT)
        present_freeze["freeze_authority"] = "FixtureFreezeAuthority"
        missing_creator = _passing_facts(settings.all(), PASS_MINT)
        missing_creator["creator"] = None
        missing_creator["creator_balance"] = None
        missing_creator["creator_balance_at_migration"] = None

        async def run():
            await reject(missing_freeze, "no data: freeze")
            db.clear_audit(PASS_MINT)
            await reject(present_freeze, "freeze not revoked")
            db.clear_audit(PASS_MINT)
            await reject(missing_creator, "no data: dev")

        asyncio.run(run())
        assert db.signals_since("1970") == []
        assert db.open_positions() == []
        assert migration_frame({"message": "Subscribed to 'migration' events."}) is None
    finally:
        db.close()
