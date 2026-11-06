import asyncio
from decimal import Decimal

import pytest

from agents.risk import Risk
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database
from fees import CONSERVATIVE, LOWEST, fee_usd, paper_pnl, round_trip_fees

# Published lowest-fee Solana route, half-up to cents.
LOWEST_BANDS = (
    (0, 0.0),
    (0.01, 0.10),
    (4.99, 0.10),
    (5, 0.10),
    (5.01, 0.10),
    (5.25, 0.11),
    (10, 0.20),
    (47.24, 0.94),
    (47.25, 0.95),
    (47.50, 0.95),
    (100, 0.95),
    (190, 0.95),
    (190.01, 0.95),
    (191, 0.96),
    (200, 1.00),
    (1000, 5.00),
)


def test_lowest_route_boundaries_and_each_side():
    for amount, expected in LOWEST_BANDS:
        assert fee_usd(amount) == expected
        assert fee_usd(amount, LOWEST) == expected
    assert fee_usd(None) == 0.0
    assert fee_usd(-1) == 0.0
    # Neighbouring published bands meet, so the shared boundary does not change the fee.
    assert fee_usd(Decimal("4.999")) == fee_usd(5) == 0.10
    assert fee_usd(Decimal("47.50")) == fee_usd(Decimal("47.51")) == 0.95
    assert fee_usd(190) == fee_usd(Decimal("190.01")) == 0.95
    assert round_trip_fees(10, 200) == fee_usd(10) + fee_usd(200) == 1.20
    assert round_trip_fees(47.50, 4) == fee_usd(47.50) + fee_usd(4) == 1.05
    # No referral discount: 0.50% of 200 stays 1.00, not 0.90.
    assert fee_usd(200) == 1.00


def test_conservative_route_is_higher_only_on_small_tickets():
    for amount in (0.01, 4.99, 10, 47.24):
        assert fee_usd(amount, CONSERVATIVE) == 0.95
        assert fee_usd(amount, CONSERVATIVE) > fee_usd(amount, LOWEST)
    for amount in (47.25, 47.50, 100, 190, 200, 1000):
        assert fee_usd(amount, CONSERVATIVE) == fee_usd(amount, LOWEST)
    assert round_trip_fees(47.50, 4, CONSERVATIVE) == 1.90
    with pytest.raises(ValueError):
        fee_usd(10, "referral")


def test_small_sale_and_partial_exit_use_the_slice_proceeds():
    entry = Decimal("1")
    size = Decimal("47.50")
    small_price = Decimal("4") / size
    pnl, fees = paper_pnl(entry, small_price, size)
    assert fees == 1.05
    assert pnl == -44.55
    _, conservative = paper_pnl(entry, small_price, size, route=CONSERVATIVE)
    assert conservative == 1.90

    full_pnl, full_fees = paper_pnl(1, 1, 100)
    same_pnl, same_fees = paper_pnl(1, 1, 100, sold_usd=100)
    assert (full_pnl, full_fees) == (same_pnl, same_fees) == (-1.90, 1.90)
    slice_pnl, slice_fees = paper_pnl(1, 1, 100, sold_usd=10)
    assert slice_fees == 0.30
    assert slice_pnl == -0.30
    assert slice_fees < full_fees

    partial_pnl, partial_fees = paper_pnl(1, 2, 100, sold_usd=25)
    assert partial_fees == 1.19
    assert partial_pnl == 23.81
    dust_pnl, dust_fees = paper_pnl(entry, small_price, size, sold_usd=10)
    assert dust_fees == 0.30
    assert dust_pnl == -9.46
    with pytest.raises(ValueError):
        paper_pnl(1, 1, 100, sold_usd=101)


def test_float_ratio_does_not_drop_a_half_cent_fee():
    # 2% of 5.75 USDC is 0.115, which rounds half-up to 0.12.
    # The float ratio of that notional lands just under 5.75.
    exit_price = float(Decimal("5.75") * Decimal("0.001") / Decimal("47.50"))
    _pnl, fees = paper_pnl(0.001, exit_price, 47.50)
    assert fees == 1.07


def test_default_route_stays_lowest_until_configured(tmp_path):
    db = Database(tmp_path / "desk.db")
    try:
        settings = DeskSettings(db)
        assert settings.get("desk.fee_route") == LOWEST
        mint = "ReplayMint1111111111111111111111111"
        db.add_position(
            {
                "mint": mint,
                "opened_at": iso(utc_now()),
                "entry_price": 1,
                "size_usd": 47.50,
                "peak_price": 1,
                "stop_price": 0.94,
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
                "name": "Replay",
            }
        )
        price = 4 / 47.50
        risk = Risk(db, settings, Bus(), None)

        async def close():
            await risk.exit(dict(db.open_positions()[0]), price, "stop", utc_now())

        asyncio.run(close())
        lowest_fees = db.positions_since("1970")[0]["fees_usd"]
        assert lowest_fees == paper_pnl(1, price, 47.50)[1]
        assert lowest_fees < paper_pnl(1, price, 47.50, route=CONSERVATIVE)[1]

        ok, _reply = settings.set("desk.fee_route", "conservative", "test")
        assert ok is True
        assert settings.get("desk.fee_route") == CONSERVATIVE
        db.add_position(
            {
                "mint": "ReplayMint2222222222222222222222222",
                "opened_at": iso(utc_now()),
                "entry_price": 1,
                "size_usd": 47.50,
                "peak_price": 1,
                "stop_price": 0.94,
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
                "name": "Replay",
            }
        )

        async def close_conservative():
            row = [item for item in db.open_positions() if item["mint"].endswith("2")][0]
            await risk.exit(dict(row), price, "stop", utc_now())

        asyncio.run(close_conservative())
        stored = [item["fees_usd"] for item in db.positions_since("1970") if item["mint"].endswith("2")][0]
        assert stored == paper_pnl(1, price, 47.50, route=CONSERVATIVE)[1]
        assert stored > lowest_fees
    finally:
        db.close()
