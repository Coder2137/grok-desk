"""Risk. Decides exits and sends a Sell button. Never places an order."""

import asyncio
from datetime import datetime, timezone

from config import iso, parse_iso, utc_now
from fees import paper_pnl
from links import coin_url
from logutil import file_logger, log

LOG = file_logger("risk", "risk.log")


def stop_price(entry: float, stop_pct: float) -> float:
    return float(entry) * (1 - float(stop_pct) / 100.0)


def trail_line(peak: float, trail_pct: float) -> float:
    return float(peak) * (1 - float(trail_pct) / 100.0)


def apply_stop_pct(position: dict, new_stop_pct: float) -> dict:
    """A looser stop never moves an open stop down. A tighter one does."""
    candidate = stop_price(position["entry_price"], new_stop_pct)
    if candidate > float(position["stop_price"]) + 1e-12:
        position["stop_price"] = candidate
        position["stop_pct"] = float(new_stop_pct)
    return position


def poll_position(position: dict, price: float, now: datetime) -> str | None:
    """One poll. Rules run in order. Returns stop, trail, timeout, or None."""
    price = float(price)
    position["peak_price"] = max(float(position["peak_price"]), price)
    entry = float(position["entry_price"])
    if price <= stop_price(entry, position["stop_pct"]) + 1e-12:
        return "stop"
    if price >= entry * (1 + float(position["trail_start_pct"]) / 100.0):
        position["trail_active"] = 1
    if position["trail_active"] and price <= trail_line(position["peak_price"], position["trail_pct"]) + 1e-12:
        return "trail"
    opened = position["opened_at"]
    if isinstance(opened, str):
        opened = parse_iso(opened)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    hours = (now.astimezone(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds() / 3600.0
    if hours >= float(position["timeout_h"]) and price < entry * (1 + float(position["timeout_min_gain_pct"]) / 100.0):
        return "timeout"
    return None


def exit_copy(mode: str, reason: str, name: str, pnl: float, pct: float, stop_pct: float, timeout_h: float) -> str:
    prefix = "PAPER · " if mode == "PAPER" else ""
    if reason == "trail":
        body = f"Sold {name} · +${pnl:.2f} (+{pct:.2f}%)"
    elif reason == "stop":
        body = f"{name} hit the -{stop_pct:g}% stop · -${abs(pnl):.2f}"
    elif reason == "timeout":
        body = f"Sell {name}, dead money · {pct:.2f}% in {timeout_h:g}h"
    elif reason == "pulled":
        body = f"Sell {name}, liquidity pulled"
    else:
        body = f"Sell {name}"
    return prefix + body


class Risk:
    def __init__(self, db, settings, bus, dex_reader) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.dex_reader = dex_reader
        self.missed_polls: dict[str, int] = {}
        self.repush: set[str] = set()
        self._backed_off = False

    async def run(self) -> None:
        for row in self.db.open_positions():
            self.bus.open_mints.add(row["mint"])
        while True:
            await self._drain()
            try:
                if self.db.open_positions():
                    await self.poll()
            except Exception as exc:
                log(LOG, f"risk error {exc.__class__.__name__}")
            self.db.heartbeat("risk")
            await asyncio.sleep(int(self.settings.get("risk.poll_s")))

    async def _drain(self) -> None:
        while True:
            try:
                message = self.bus.risk.get_nowait()
            except asyncio.QueueEmpty:
                return
            mint = message.get("mint")
            if mint:
                self.bus.open_mints.add(mint)

    def refuse_pause(self) -> bool:
        return bool(self.db.open_positions())

    def tighten_open(self, new_stop_pct: float) -> None:
        for row in self.db.open_positions():
            position = dict(row)
            apply_stop_pct(position, new_stop_pct)
            if float(position["stop_pct"]) != float(row["stop_pct"]) or float(position["stop_price"]) != float(row["stop_price"]):
                self.db.update_position(
                    row["id"],
                    stop_pct=position["stop_pct"],
                    stop_price=position["stop_price"],
                )

    async def poll(self) -> None:
        rows = list(self.db.open_positions())
        if not rows:
            return
        mints = [row["mint"] for row in rows]
        try:
            pairs = await self.dex_reader(mints)
        except Exception as exc:
            name = exc.__class__.__name__
            if "Rate" in name or "429" in str(exc):
                log(LOG, "429 backoff 30s")
                await asyncio.sleep(30)
                return
            log(LOG, f"poll failed {name}")
            pairs = []
        by_mint = {}
        for pair in pairs:
            mint = (pair or {}).get("mint")
            if mint:
                by_mint[mint] = pair
        now = utc_now()
        for row in rows:
            position = dict(row)
            quote = by_mint.get(position["mint"])
            price = None if not quote else quote.get("priceUsd")
            volume = None if not quote else quote.get("volumeM5")
            liq = None if not quote else quote.get("liquidityUsd")
            if price is not None:
                self.db.add_price(position["mint"], price, volume, iso(now))
                self.missed_polls[position["mint"]] = 0
                self.bus.market[position["mint"]] = quote
            else:
                self.missed_polls[position["mint"]] = self.missed_polls.get(position["mint"], 0) + 1
            if liq == 0 and position["status"] == "open":
                last = self.db.latest_price(position["mint"])
                last_price = float(last["price"]) if last else float(position["entry_price"])
                await self.exit(position, last_price, "pulled", now)
                continue
            if price is None:
                if self.missed_polls[position["mint"]] >= 4:
                    await self.bus.events.put(
                        {
                            "type": "sell",
                            "mint": position["mint"],
                            "text": f"{position['name'] or position['mint']}: no price for 1 min",
                            "link": coin_url(position["mint"], "solana"),
                            "sold": False,
                        }
                    )
                    self.missed_polls[position["mint"]] = 0
                self._log_poll(position, None, "no price")
                continue
            action = poll_position(position, price, now)
            self.db.update_position(
                position["id"],
                peak_price=position["peak_price"],
                trail_active=int(position["trail_active"] or 0),
            )
            self._log_poll(position, price, action or "hold")
            if action and position["status"] == "open":
                await self.exit(position, price, action, now)
            elif position["status"] == "exiting":
                await self._nudge_live(position)

    def _log_poll(self, position, price, action) -> None:
        stop_line = stop_price(position["entry_price"], position["stop_pct"])
        trail = trail_line(position["peak_price"], position["trail_pct"]) if position["trail_active"] else None
        log(
            LOG,
            f"{position['mint']} price {price} peak {position['peak_price']} stop {stop_line} trail {trail} action {action}",
        )

    async def exit(self, position, price, reason, now) -> None:
        mode = position["mode"]
        entry = float(position["entry_price"])
        pnl, fees = paper_pnl(entry, price, position["size_usd"])
        pct = ((float(price) / entry) - 1) * 100 if entry else 0
        name = position["name"] or position["mint"]
        text = exit_copy(mode, reason, name, pnl, pct, position["stop_pct"], position["timeout_h"])
        link = coin_url(position["mint"], "solana")
        if mode == "PAPER" or reason == "pulled":
            self.db.update_position(
                position["id"],
                closed_at=iso(now),
                exit_price=float(price),
                exit_reason=reason,
                pnl_usd=pnl,
                fees_usd=fees,
                status="closed",
                peak_price=position["peak_price"],
                trail_active=int(position["trail_active"] or 0),
            )
            self.bus.open_mints.discard(position["mint"])
            self._activity(position, reason, pnl, pct)
        else:
            self.db.update_position(
                position["id"],
                status="exiting",
                exit_price=float(price),
                exit_reason=reason,
                pnl_usd=pnl,
                fees_usd=fees,
                peak_price=position["peak_price"],
                trail_active=int(position["trail_active"] or 0),
            )
        await self.bus.events.put(
            {"type": "sell", "mint": position["mint"], "text": text, "link": link, "sold": True}
        )

    async def _nudge_live(self, position) -> None:
        mint = position["mint"]
        if mint in self.repush:
            return
        opened_exit = position["closed_at"]
        # confirm window is measured from the first sell push, stored loosely via missed polls
        self.missed_polls[mint] = self.missed_polls.get(mint, 0) + 1
        window_polls = max(1, int(int(self.settings.get("risk.confirm_window_min")) * 60 / int(self.settings.get("risk.poll_s"))))
        if self.missed_polls[mint] < window_polls:
            return
        self.repush.add(mint)
        name = position["name"] or mint
        await self.bus.events.put(
            {
                "type": "sell",
                "mint": mint,
                "text": f"{name} still open",
                "link": coin_url(mint, "solana"),
                "sold": True,
            }
        )

    def mark_sold(self, mint: str) -> None:
        row = self.db.open_position_for(mint)
        if not row:
            return
        if row["mode"] != "LIVE":
            return
        self.db.update_position(row["id"], status="closed", closed_at=iso(utc_now()))
        self.bus.open_mints.discard(mint)
        pct = 0
        if row["entry_price"] and row["exit_price"]:
            pct = ((float(row["exit_price"]) / float(row["entry_price"])) - 1) * 100
        self._activity(dict(row), row["exit_reason"] or "sold", float(row["pnl_usd"] or 0), pct)

    def _activity(self, position, reason, pnl, pct) -> None:
        name = position["name"] or position["mint"]
        if reason == "stop":
            self.db.add_activity(
                "stopped",
                position["mint"],
                "Risk",
                f"Stopped {name}",
                f"Risk · -{float(position['stop_pct']):g}% stop",
                f"-${abs(pnl):.2f}",
            )
        elif reason == "trail":
            self.db.add_activity(
                "sold",
                position["mint"],
                "Risk",
                f"Sold {name}",
                "Risk · trailing stop",
                f"+${pnl:.2f} / +{pct:.2f}%",
            )
        else:
            self.db.add_activity(
                "sold",
                position["mint"],
                "Risk",
                f"Sold {name}",
                f"Risk · {reason}",
                f"${pnl:.2f}",
            )
