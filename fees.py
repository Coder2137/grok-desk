"""FOMO Solana fee model for PAPER PnL.

Official page (Trading fees on fomo, help.fomo.family article 14436214,
read 2026-11-06) says the lowest-fee Solana route is:

- under 5 USDC: 0.10 USDC
- 5 to 47.50 USDC: 2%
- 47.50 to 190 USDC: 0.95 USDC
- 190 USDC and up: 0.50%

At 5, 47.50, and 190 the neighbouring bands meet, so the shared boundary
does not change the fee. Other routes are 0.50% with a 0.95 USDC minimum.
The route is chosen when the user taps Buy or Sell on FOMO, which this desk
never does, so PAPER uses the published lowest-fee bands.

No referral discount is applied. Gas is 0 because fomo pays Solana network
gas, priority fees, and token rent. The operator should still read the fee
on the FOMO confirmation screen before tapping.
"""

from decimal import Decimal, ROUND_HALF_UP


def _cents(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fee_usd(amount: float) -> float:
    """Fee for one side of a Solana ticket. Gas is not added."""
    if amount is None:
        return 0.0
    size = Decimal(str(amount))
    if size <= 0:
        return 0.0
    if size < Decimal("5"):
        return 0.10
    if size <= Decimal("47.50"):
        return _cents(size * Decimal("0.02"))
    if size <= Decimal("190"):
        return 0.95
    return _cents(size * Decimal("0.005"))


GAS_USD = 0.0


def round_trip_fees(entry_notional: float, exit_notional: float) -> float:
    """Both sides, no discount, no gas."""
    return _cents(Decimal(str(fee_usd(entry_notional) + fee_usd(exit_notional) + GAS_USD)))


def paper_pnl(entry_price: float, exit_price: float, size_usd: float) -> tuple[float, float]:
    """Return (pnl_usd, fees_usd). Exit notional scales with the price move."""
    if not entry_price or entry_price <= 0 or size_usd is None:
        return 0.0, 0.0
    exit_notional = float(size_usd) * (float(exit_price) / float(entry_price))
    fees = round_trip_fees(float(size_usd), exit_notional)
    pnl = exit_notional - float(size_usd) - fees
    return _cents(Decimal(str(pnl))), fees
