"""Executor. Arithmetic and one button. Never places an order and never builds a FOMO URL."""

import asyncio

from config import iso, ticket_check, utc_now
from links import coin_url
from logutil import file_logger, log

LOG = file_logger("executor", "executor.log")


def round_cents(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def compute_size(stack, free, pool_liq, stack_pct, max_pool_pct, min_ticket, below_min):
    """Return (size or None, reason, raw, cap)."""
    raw = round_cents(float(stack) * float(stack_pct) / 100.0)
    size = raw
    cap = None
    pool_cap = round_cents(float(pool_liq) * float(max_pool_pct) / 100.0)
    if size > pool_cap:
        size = pool_cap
        cap = "pool"
    if size < float(min_ticket):
        if below_min == "bump":
            if float(free) >= float(min_ticket):
                return round_cents(min_ticket), "bump", raw, cap or "bump"
            return None, "no free stack", raw, cap
        return None, "under $47.50", raw, cap
    if size > float(free):
        return None, "no free stack", raw, cap
    return round_cents(size), cap, raw, cap


def recheck(scanner_liq, now_liq, analyst_price, now_price, liq_drop_pct, price_up_pct):
    if now_price is None:
        return "no price"
    if scanner_liq and now_liq is not None:
        if float(now_liq) < float(scanner_liq) * (1 - float(liq_drop_pct) / 100.0):
            return "liquidity dropping"
    if analyst_price and float(now_price) > float(analyst_price) * (1 + float(price_up_pct) / 100.0):
        return "ran before the push"
    return None


def push_text(mode, name, size, stack, score, stop_pct, trail_pct) -> str:
    pct = (float(size) / float(stack) * 100) if stack else 0
    pct_txt = f"{pct:.2f}".rstrip("0").rstrip(".")
    prefix = "PAPER · " if mode == "PAPER" else ""
    return (
        f"{prefix}Buy {name} · ${size:.2f} · {pct_txt}% of stack\n"
        f"Auditor 8/8 · Analyst {score} · stop -{stop_pct:g}% · trail {trail_pct:g}%"
    )


def paper_start_warning(stack, stack_pct, min_ticket, below_min) -> str | None:
    return ticket_check(stack, stack_pct, min_ticket, below_min)


class Executor:
    def __init__(self, db, settings, bus) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self._waiters: dict[str, asyncio.Future] = {}

    async def run(self) -> None:
        while True:
            message = await self.bus.executor.get()
            try:
                if not self.settings.get("agent.executor.paused"):
                    await self.handle(message)
            except Exception as exc:
                log(LOG, f"executor error {exc.__class__.__name__}")
            self.db.heartbeat("executor")

    def free_stack(self, settings) -> float:
        mode = settings["desk.mode"]
        stack = float(settings["desk.stack"])
        open_rows = self.db.open_positions()
        locked = sum(float(row["size_usd"] or 0) for row in open_rows)
        if mode == "PAPER":
            equity = float(settings["desk.night_start_equity"] or settings["desk.paper_start_stack"])
            for row in self.db.positions_since(settings["desk.night_start"] or "1970"):
                if row["status"] == "closed" and row["pnl_usd"] is not None:
                    equity += float(row["pnl_usd"])
            return max(0.0, equity - locked)
        return max(0.0, stack - locked)

    async def handle(self, message: dict) -> None:
        settings = self.settings.all()
        mint = message["mint"]
        since = settings["desk.night_start"] or "1970"
        if self.db.signal_tonight(mint, since):
            return
        if self.db.open_position_for(mint):
            return
        if self.db.skipped(mint, iso(utc_now())):
            return
        stack = float(settings["desk.stack"])
        free = self.free_stack(settings)
        pool_liq = float(message.get("liquidityUsd") or 0)
        size, reason, raw, cap = compute_size(
            stack,
            free,
            pool_liq,
            settings["executor.stack_pct"],
            settings["executor.max_pool_pct"],
            settings["executor.min_ticket_usd"],
            settings["executor.below_min"],
        )
        mode = settings["desk.mode"]
        link = coin_url(mint, "solana")
        if size is None:
            self.db.add_signal(
                {
                    "mint": mint,
                    "ts": iso(utc_now()),
                    "price": message.get("analystPrice"),
                    "size_usd": None,
                    "stack_pct": settings["executor.stack_pct"],
                    "link": link,
                    "mode": mode,
                    "status": "skipped",
                    "reason": reason,
                }
            )
            log(LOG, f"{mint} stack {stack} raw {raw} cap {cap} final none mode {mode} status skipped {reason} {link}")
            return
        now_price = (message.get("dex") or {}).get("priceUsd")
        if message.get("pushPrice") is not None:
            now_price = message["pushPrice"]
        problem = recheck(
            message.get("liquidityUsd"),
            message.get("nowLiquidity", message.get("liquidityUsd")),
            message.get("analystPrice"),
            now_price,
            settings["executor.recheck_liq_drop_pct"],
            settings["executor.recheck_price_up_pct"],
        )
        if problem:
            self.db.add_signal(
                {
                    "mint": mint,
                    "ts": iso(utc_now()),
                    "price": now_price,
                    "size_usd": size,
                    "stack_pct": size / stack * 100 if stack else 0,
                    "link": link,
                    "mode": mode,
                    "status": "skipped",
                    "reason": problem,
                }
            )
            log(LOG, f"{mint} stack {stack} raw {raw} cap {cap} final {size} mode {mode} status skipped {problem} {link}")
            return
        signal_id = self.db.add_signal(
            {
                "mint": mint,
                "ts": iso(utc_now()),
                "price": now_price,
                "size_usd": size,
                "stack_pct": size / stack * 100 if stack else 0,
                "link": link,
                "mode": mode,
                "status": "sent",
                "reason": None,
            }
        )
        text = push_text(
            mode,
            message.get("name") or mint,
            size,
            stack,
            message.get("score"),
            settings["risk.stop_pct"],
            settings["risk.trail_pct"],
        )
        pushed = await self._push(mint, text, link)
        log(
            LOG,
            f"{mint} stack {stack} raw {raw} cap {cap} final {size} mode {mode} status sent {link}",
        )
        if mode == "PAPER":
            if not pushed:
                log(LOG, f"{mint} push failed")
            await self.fill(mint, signal_id, now_price, size, mode, message.get("name"), settings)
            return
        if not pushed:
            log(LOG, f"{mint} push failed")
            return
        asyncio.create_task(self._confirm(mint, signal_id, now_price, size, message.get("name"), settings))

    async def _push(self, mint, text, link) -> bool:
        for _attempt in range(3):
            loop = asyncio.get_running_loop()
            ack = loop.create_future()
            await self.bus.events.put(
                {"type": "buy", "mint": mint, "text": text, "link": link, "ack": ack}
            )
            try:
                ok = await asyncio.wait_for(ack, timeout=20)
            except asyncio.TimeoutError:
                ok = False
            if ok:
                return True
            await asyncio.sleep(1)
        return False

    async def _confirm(self, mint, signal_id, price, size, name, settings) -> None:
        window = int(settings["executor.confirm_window_min"]) * 60
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters[mint] = future
        try:
            action = await asyncio.wait_for(future, timeout=window)
        except asyncio.TimeoutError:
            action = "missed"
        self._waiters.pop(mint, None)
        if action == "bought":
            await self.fill(mint, signal_id, price, size, "LIVE", name, self.settings.all())
        elif action == "skip":
            self.skip(mint)
            self.db.update_signal(signal_id, status="skipped", reason="skip")
        else:
            self.db.update_signal(signal_id, status="missed", reason="missed")

    def on_command(self, action: str, mint: str) -> None:
        future = self._waiters.get(mint)
        if future and not future.done():
            future.set_result(action)
            return
        if action == "bought":
            row = self.db.signal_tonight(mint, self.settings.get("desk.night_start") or "1970")
            if row and row["status"] == "sent" and row["mode"] == "LIVE":
                coin = self.db.coin(mint)
                asyncio.create_task(
                    self.fill(
                        mint,
                        row["id"],
                        row["price"],
                        row["size_usd"],
                        "LIVE",
                        coin["name"] if coin else mint,
                        self.settings.all(),
                    )
                )
        elif action == "skip":
            self.skip(mint)

    def skip(self, mint: str) -> None:
        _start, end = self.settings.night_bounds()
        self.db.add_skip(mint, iso(end))

    async def fill(self, mint, signal_id, price, size, mode, name, settings) -> None:
        if self.db.open_position_for(mint):
            return
        self.db.update_signal(signal_id, status="bought")
        entry = float(price)
        stop_pct = float(settings["risk.stop_pct"])
        position = {
            "mint": mint,
            "opened_at": iso(utc_now()),
            "entry_price": entry,
            "size_usd": float(size),
            "peak_price": entry,
            "stop_price": entry * (1 - stop_pct / 100.0),
            "trail_active": 0,
            "closed_at": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_usd": None,
            "fees_usd": None,
            "mode": mode,
            "stop_pct": stop_pct,
            "trail_start_pct": float(settings["risk.trail_start_pct"]),
            "trail_pct": float(settings["risk.trail_pct"]),
            "timeout_h": float(settings["risk.timeout_h"]),
            "timeout_min_gain_pct": float(settings["risk.timeout_min_gain_pct"]),
            "status": "open",
            "name": name,
        }
        position_id = self.db.add_position(position)
        stack = float(settings["desk.stack"])
        pct = float(size) / stack * 100 if stack else 0
        pct_txt = f"{pct:.2f}".rstrip("0").rstrip(".")
        when = utc_now().strftime("%H:%M")
        self.db.add_activity(
            "bought",
            mint,
            "Executor",
            f"Bought {name or mint}",
            f"Executor · {when}",
            f"${float(size):.2f} · {pct_txt}% of stack",
        )
        self.bus.open_mints.add(mint)
        await self.bus.risk.put({"position_id": position_id, "mint": mint})
        log(LOG, f"{mint} filled {size} {mode} {coin_url(mint, 'solana')}")
