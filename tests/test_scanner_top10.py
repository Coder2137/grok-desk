import asyncio
from datetime import timedelta

from agents.scanner import Scanner, top10_from_holders
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database

MINT = "FixtureTopMint11111111111111111111111111"
POOL = "FixturePair1111111111111111111111111111111"


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    return db, DeskSettings(db), Bus()


def _pair(mint, liquidity, h1):
    return {
        "dexId": "pumpswap",
        "baseToken": {"address": mint, "name": "Fixture Coin", "symbol": "FIX"},
        "pairAddress": POOL,
        "priceUsd": "0.001",
        "liquidity": {"usd": liquidity},
        "volume": {"m5": 4000, "h1": 30000, "h24": 90000},
        "txns": {"m5": {"buys": 20, "sells": 10}, "h1": {"buys": 180, "sells": 40}},
        "priceChange": {"m5": -1, "h1": h1, "h24": 12},
    }


def _holders(pool_pct, holder_pct, count):
    rows = [{"owner": POOL, "address": "LpTokenAccount", "pct": pool_pct}]
    rows.extend({"owner": f"Wallet{index}", "address": f"Acct{index}", "pct": holder_pct} for index in range(count))
    rows.append({"owner": "MissingPct", "address": "AcctMissing", "pct": None})
    return rows


def _arm(scanner, minutes):
    scanner.waiting[MINT] = {
        "mint": MINT,
        "pool": None,
        "pool_label": "pump-amm",
        "migratedAt": iso(utc_now() - timedelta(minutes=minutes)),
        "creator": None,
    }
    scanner.bus.waiting.add(MINT)


def test_top10_excludes_the_pool_and_keeps_null_pct_out():
    # Pool holds 60. Eleven wallets hold 2 each. The null pct is ignored.
    # The ten largest remaining accounts are 20% of supply.
    assert top10_from_holders(_holders(60, 2, 11), POOL, None) == 20
    assert top10_from_holders(None, POOL, None) is None
    assert top10_from_holders([], POOL, None) is None


def test_good_top10_reaches_the_auditor(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)
        _arm(scanner, 20)
        holders = _holders(60, 2, 11)

        async def fetch(_mint):
            return {"topHolders": holders}, None

        scanner._fetch_report = fetch

        async def run():
            await scanner.poll_once(pairs=[_pair(MINT, 80_000, 4)])
            await asyncio.sleep(0)

        asyncio.run(run())
        row = db.latest_scan(MINT)
        assert row["verdict"] == "pass"
        assert row["top10_pct"] == 20
        assert row["reason"] is None
        message = bus.auditor.get_nowait()
        assert message["mint"] == MINT
        assert message["top10Pct"] == 20
        assert MINT not in scanner.waiting
    finally:
        db.close()


def test_high_top10_is_rejected(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)
        _arm(scanner, 20)

        async def fetch(_mint):
            return {"topHolders": _holders(40, 3, 12)}, None

        scanner._fetch_report = fetch
        asyncio.run(scanner.poll_once(pairs=[_pair(MINT, 80_000, 4)]))
        row = db.latest_scan(MINT)
        assert row["verdict"] == "kill"
        assert row["top10_pct"] == 30
        assert row["reason"] == "top 10 hold 30%"
        assert bus.auditor.empty()
        assert MINT not in scanner.waiting
    finally:
        db.close()


def test_holder_fetch_failure_stays_null_and_retries_inside_the_wait(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)
        _arm(scanner, 20)
        calls = {"n": 0}

        async def fetch(_mint):
            calls["n"] += 1
            if calls["n"] == 1:
                return None, "rugcheck http 429"
            return {"topHolders": _holders(60, 2, 11)}, None

        scanner._fetch_report = fetch

        async def run():
            await scanner.poll_once(pairs=[_pair(MINT, 80_000, 4)])
            assert MINT in scanner.waiting
            assert db.latest_scan(MINT) is None
            assert scanner.waiting[MINT]["top10_error"] == "rugcheck http 429"
            await scanner.poll_once(pairs=[_pair(MINT, 80_000, 4)])
            await asyncio.sleep(0)

        asyncio.run(run())
        assert calls["n"] == 2
        row = db.latest_scan(MINT)
        assert row["verdict"] == "pass"
        assert row["top10_pct"] == 20
        assert bus.auditor.get_nowait()["mint"] == MINT
    finally:
        db.close()


def test_holder_fetch_failure_after_the_wait_records_the_cause(tmp_path):
    db, settings, bus = _desk(tmp_path)
    try:
        scanner = Scanner(db, settings, bus)
        _arm(scanner, 61)

        async def fetch(_mint):
            return None, "rugcheck http 429"

        scanner._fetch_report = fetch
        asyncio.run(scanner.poll_once(pairs=[_pair(MINT, 80_000, 4)]))
        row = db.latest_scan(MINT)
        assert row["verdict"] == "kill"
        assert row["top10_pct"] is None
        assert row["reason"] == "top10 unavailable: rugcheck http 429"
        assert bus.auditor.empty()
        assert MINT not in scanner.waiting
    finally:
        db.close()
