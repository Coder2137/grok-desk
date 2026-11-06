from agents.executor import compute_size
from config import ticket_check


def test_small_stack_bumps_or_skips():
    size, reason, raw, _cap = compute_size(200, 200, 100_000, 8, 2, 47.50, "bump")
    assert raw == 16
    assert size == 47.50
    assert reason == "bump"
    skipped, why, raw_skip, _cap = compute_size(200, 200, 100_000, 8, 2, 47.50, "skip")
    assert raw_skip == 16
    assert skipped is None
    assert why == "under $47.50"


def test_paper_start_skip_warns():
    note = ticket_check(200, 8, 47.50, "skip")
    assert note == "every coin will be skipped, stack under $593.75"
    bump = ticket_check(200, 8, 47.50, "bump")
    assert bump.startswith("tickets bumped to $47.50")


def test_pool_caps_size():
    size, reason, raw, cap = compute_size(20_000, 20_000, 40_000, 8, 2, 47.50, "bump")
    assert raw == 1600
    assert size == 800
    assert cap == "pool"
    assert reason == "pool"
