"""FOMO Solana fee estimates for PAPER PnL.

Official page (Trading fees on fomo, help.fomo.family article 14436214,
re-read 2026-11-06): the lowest-fee Solana route is 0.10 USDC under 5,
2% from 5 to 47.50, 0.95 USDC from 47.50 to 190, and 0.50% from 190 up.
Other Solana routes are 0.50% with a 0.95 USDC minimum, and which route
is available changes from trade to trade. fomo covers Solana gas, priority
fees, and token rent. This desk never taps Buy or Sell, so these figures
are estimates of that published schedule, not the quote on the confirmation
screen. No referral discount is applied.

The default route is the lowest-fee schedule. ``conservative`` uses the
other-route schedule so a small sell is not priced as 0.10 when that route
may be unavailable. After half-up cent rounding the two schedules match from
47.25 USDC up, because 2% of 47.25 is 0.945, which rounds to the 0.95 flat
fee. The published unrounded bands meet at 47.50. The 47.50 ticket floor is
the desk's own rule, not a fomo minimum.

Amounts are taken to 6 decimal places (USDC) before the schedule so binary
float noise cannot round a half-cent fee down. The fee itself is rounded
half-up to cents.
"""

from decimal import Decimal, ROUND_HALF_UP

LOWEST = "lowest"
CONSERVATIVE = "conservative"
_USDC = Decimal("0.000001")
_CENT = Decimal("0.01")
GAS_USD = 0.0


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _amount(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _usdc(value: Decimal) -> Decimal:
    return value.quantize(_USDC, rounding=ROUND_HALF_UP)


def _normalize_route(route: str) -> str:
    name = (route or LOWEST).strip().lower()
    if name not in (LOWEST, CONSERVATIVE):
        raise ValueError("route")
    return name


def _fee(amount: Decimal, route: str) -> Decimal:
    """One side. Gas is not included. ``amount`` is a USDC notional."""
    size = _usdc(amount)
    if size <= 0:
        return Decimal("0.00")
    name = _normalize_route(route)
    if name == CONSERVATIVE:
        pct = _cents(size * Decimal("0.005"))
        if pct < Decimal("0.95"):
            return Decimal("0.95")
        return pct
    if size < Decimal("5"):
        return Decimal("0.10")
    if size <= Decimal("47.50"):
        return _cents(size * Decimal("0.02"))
    if size <= Decimal("190"):
        return Decimal("0.95")
    return _cents(size * Decimal("0.005"))


def fee_usd(amount: float, route: str = LOWEST) -> float:
    """Fee for one side of a Solana ticket. Gas is not added."""
    if amount is None:
        return 0.0
    return float(_fee(_amount(amount), route))


def round_trip_fees(entry_notional: float, exit_notional: float, route: str = LOWEST) -> float:
    """Both sides, no discount, no gas."""
    total = _fee(_amount(0 if entry_notional is None else entry_notional), route)
    total += _fee(_amount(0 if exit_notional is None else exit_notional), route)
    total += Decimal(str(GAS_USD))
    return float(_cents(total))


def paper_pnl(
    entry_price: float,
    exit_price: float,
    size_usd: float,
    *,
    sold_usd: float | None = None,
    route: str = LOWEST,
) -> tuple[float, float]:
    """Return (pnl_usd, fees_usd).

    Omit ``sold_usd`` for a full close: both fees use the whole position,
    and the exit notional scales with the price move. Pass ``sold_usd`` for
    a partial-exit estimate of that slice only. The entry fee is the
    pro-rata share of the fee on the original ticket, and the sell fee is
    charged on the slice proceeds. Risk still closes the whole position.
    """
    if entry_price is None or size_usd is None:
        return 0.0, 0.0
    entry = _amount(entry_price)
    size = _usdc(_amount(size_usd))
    if entry <= 0 or size <= 0:
        return 0.0, 0.0
    sold = size if sold_usd is None else _usdc(_amount(sold_usd))
    if sold <= 0 or sold > size:
        raise ValueError("sold")
    proceeds = _usdc(sold * _amount(exit_price) / entry)
    entry_fee = _fee(size, route) * (sold / size)
    sell_fee = _fee(proceeds, route)
    fees = _cents(entry_fee + sell_fee)
    pnl = _cents(proceeds - sold - fees)
    return float(pnl), float(fees)
