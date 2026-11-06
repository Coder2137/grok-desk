from datetime import timedelta

from agents.risk import apply_stop_pct, poll_position, stop_price
from config import utc_now


def _position(stop_pct=6):
    entry = 100.0
    return {
        "entry_price": entry,
        "peak_price": entry,
        "stop_price": stop_price(entry, stop_pct),
        "stop_pct": stop_pct,
        "trail_start_pct": 20,
        "trail_pct": 12,
        "trail_active": 0,
        "opened_at": utc_now(),
        "timeout_h": 3,
        "timeout_min_gain_pct": 5,
    }


def test_trail_after_a_giveback():
    position = _position()
    first = poll_position(position, 125, utc_now())
    assert first is None
    assert position["trail_active"] == 1
    assert position["peak_price"] == 125
    second = poll_position(position, 125 * 0.87, utc_now())
    assert second == "trail"


def test_stop_and_timeout():
    position = _position()
    assert poll_position(position, 100 * 0.939, utc_now()) == "stop"
    aged = _position()
    aged["opened_at"] = utc_now() - timedelta(hours=3)
    assert poll_position(aged, 104, utc_now()) == "timeout"


def test_looser_stop_does_not_move_an_open_position():
    old = _position(6)
    apply_stop_pct(old, 8)
    assert old["stop_pct"] == 6
    assert old["stop_price"] == stop_price(100, 6)
    new = _position(8)
    assert poll_position(new, 100 * 0.919, utc_now()) == "stop"
    assert new["stop_pct"] == 8
    assert old["stop_pct"] == 6
    tighter = _position(6)
    apply_stop_pct(tighter, 4)
    assert tighter["stop_pct"] == 4
    assert tighter["stop_price"] == stop_price(100, 4)
