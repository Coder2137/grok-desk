"""Owner-only Telegram long polling. Buttons open FOMO. This process never places an order."""

import asyncio
import os

import httpx

from config import iso, utc_now
from links import coin_url
from logutil import file_logger, log, redact

LOG = file_logger("telegram", "telegram.log")

HELP = (
    "buy X\n"
    "sell X\n"
    "why X\n"
    "pause Scanner\n"
    "resume Scanner\n"
    "set stop to -8%\n"
    "stack 1240\n"
    "status\n"
    "paper\n"
    "LIVE\n"
    "report\n"
    "test"
)

AGENTS = {
    "scanner": "agent.scanner.paused",
    "auditor": "agent.auditor.paused",
    "analyst": "agent.analyst.paused",
    "executor": "agent.executor.paused",
    "risk": "agent.risk.paused",
}


class Telegram:
    def __init__(self, db, settings, bus, executor, risk) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.executor = executor
        self.risk = risk
        self.token = os.environ.get("TG_TOKEN") or ""
        self.offset = 0
        self._confirm_auditor = False

    def enabled(self) -> bool:
        return os.environ.get("DESK_TELEGRAM") != "0"

    async def run(self) -> None:
        await asyncio.gather(self._poll(), self._events())

    async def _poll(self) -> None:
        if not self.enabled():
            log(LOG, "telegram disabled")
            while True:
                await asyncio.sleep(3600)
        if not self.token:
            log(LOG, "telegram token missing")
            while True:
                await asyncio.sleep(3600)
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                try:
                    response = await client.get(
                        f"https://api.telegram.org/bot{self.token}/getUpdates",
                        params={"timeout": 50, "offset": self.offset},
                    )
                    body = response.json()
                except Exception as exc:
                    log(LOG, f"poll {redact(str(exc.__class__.__name__))}")
                    await asyncio.sleep(2)
                    continue
                for update in body.get("result") or []:
                    self.offset = int(update["update_id"]) + 1
                    await self._update(client, update)
                self.db.heartbeat("telegram")

    async def _events(self) -> None:
        if not self.enabled():
            while True:
                event = await self.bus.events.get()
                ack = event.get("ack")
                if ack is not None and not ack.done():
                    ack.set_result(False)
            return
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                event = await self.bus.events.get()
                ack = event.get("ack")
                try:
                    ok = await self._deliver(client, event)
                except Exception as exc:
                    log(LOG, f"send {exc.__class__.__name__}")
                    ok = False
                if ack is not None and not ack.done():
                    ack.set_result(ok)

    async def _deliver(self, client, event) -> bool:
        if not self.token:
            return False
        chat = self.settings.get("desk.owner_chat_id")
        if not chat:
            return False
        kind = event.get("type")
        if kind == "text":
            return await self._send(client, chat, event.get("text") or "")
        if kind == "buy":
            return await self._send(
                client,
                chat,
                event["text"],
                [
                    [{"text": "Buy on FOMO", "url": event["link"]}],
                    [
                        {"text": "Bought", "callback_data": f"bought:{event['mint']}"},
                        {"text": "Skip", "callback_data": f"skip:{event['mint']}"},
                    ],
                ],
            )
        if kind == "sell":
            row = [[{"text": "Sell on FOMO", "url": event["link"]}]]
            if event.get("sold", True):
                row.append([{"text": "Sold", "callback_data": f"sold:{event['mint']}"}])
            return await self._send(client, chat, event["text"], row)
        return False

    async def _send(self, client, chat, text, keyboard=None) -> bool:
        payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        response = await client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json=payload,
        )
        return response.status_code == 200

    async def _update(self, client, update) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message") or {}
            chat = str((message.get("chat") or {}).get("id") or "")
            data = query.get("data") or ""
            if not self._allowed(chat):
                return
            await self._callback(data)
            try:
                await client.post(
                    f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                    json={"callback_query_id": query.get("id")},
                )
            except Exception:
                pass
            return
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id") or "")
        text = message.get("text") or ""
        replies = self.handle_text(chat, text)
        for reply in replies:
            await self._send(client, chat, reply["text"], reply.get("keyboard"))

    def _allowed(self, chat: str) -> bool:
        owner = str(self.settings.get("desk.owner_chat_id") or "")
        return bool(owner) and chat == owner

    def _callback(self, data: str) -> None:
        if ":" not in data:
            return
        action, mint = data.split(":", 1)
        if action in ("bought", "skip"):
            self.executor.on_command(action, mint)
        elif action == "sold":
            self.risk.mark_sold(mint)

    def handle_text(self, chat: str, text: str) -> list[dict]:
        raw = (text or "").strip()
        owner = str(self.settings.get("desk.owner_chat_id") or "")
        command = raw[1:] if raw.startswith("/") else raw
        head = command.split(maxsplit=1)[0].lower() if command else ""
        if not owner:
            if head == "start":
                self.settings.db.set_setting("desk.owner_chat_id", chat, "telegram")
                return [{"text": "Grok Bot is yours. PAPER mode. " + HELP}]
            return []
        if chat != owner:
            return []
        if head == "start":
            return [{"text": HELP}]
        return self._dispatch(command)

    def _dispatch(self, command: str) -> list[dict]:
        if not command:
            return [{"text": HELP}]
        if command == "AUDITOR OFF":
            ok, reply = self.settings.set("auditor.enabled", False, "telegram")
            self._confirm_auditor = False
            return [{"text": reply if ok else reply}]
        parts = command.split(maxsplit=1)
        head = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if head in ("help", "commands"):
            return [{"text": HELP}]
        if head == "test":
            return [{"text": "Grok Bot test push"}]
        if head == "paper":
            ok, reply = self.settings.set("desk.mode", "PAPER", "telegram")
            return [{"text": reply}]
        if command == "LIVE":
            ok, reply = self.settings.set("desk.mode", "LIVE", "telegram")
            return [{"text": reply}]
        if head == "live":
            return [{"text": "Type LIVE to leave PAPER. A full PAPER night has to be in the db."}]
        if head == "status":
            return [self._status()]
        if head == "report":
            return [{"text": self._report_line()}]
        if head == "stack" and rest:
            ok, reply = self.settings.set("desk.stack", rest.split()[0], "telegram")
            return [{"text": reply}]
        if head == "pause" and rest:
            return [self._pause(rest, True)]
        if head == "resume" and rest:
            return [self._pause(rest, False)]
        if head == "buy" and rest:
            return self._buy(rest)
        if head == "sell" and rest:
            return self._sell(rest)
        if head == "why" and rest:
            return self._why(rest)
        if head == "set" and rest:
            return [self._set(rest)]
        return [{"text": HELP}]

    def _pause(self, name: str, paused: bool) -> dict:
        key = AGENTS.get(name.strip().lower())
        if not key:
            return {"text": "Unknown agent. Scanner, Auditor, Analyst, Executor, Risk."}
        if key.endswith("risk.paused") and paused and self.risk.refuse_pause():
            return {"text": "Risk stays on while a position is open."}
        ok, reply = self.settings.set(key, paused, "telegram")
        label = name.strip().title()
        state = "paused" if paused else "running"
        return {"text": f"{label} {state}" if ok else reply}

    def _set(self, rest: str) -> dict:
        text = rest.strip()
        lower = text.lower()
        if lower.startswith("stop"):
            key = "risk.stop_pct"
            raw = lower.replace("stop", " ", 1).replace("to", " ").replace("%", " ")
            raw = raw.strip().split()[0] if raw.strip() else ""
        else:
            chunks = text.replace(" to ", " ").split()
            if len(chunks) < 2:
                return {"text": HELP}
            key_name = chunks[0]
            raw = chunks[-1]
            from config import resolve_key

            key = resolve_key(key_name)
            if not key:
                return {"text": f"unknown setting {key_name}"}
        if key == "auditor.enabled" and str(raw).lower() in ("0", "false", "off", "no"):
            if not self._confirm_auditor:
                self._confirm_auditor = True
                return {
                    "text": "Turn the auditor off? Coins will skip the safety check. Send AUDITOR OFF to confirm."
                }
        ok, reply = self.settings.set(key, raw, "telegram")
        if ok and key == "risk.stop_pct":
            self.risk.tighten_open(float(self.settings.get("risk.stop_pct")))
        self._confirm_auditor = False
        return {"text": reply}

    def _resolve(self, text: str):
        matches = self.db.find_coins(text)
        if matches:
            return matches, None
        if _mintish(text.strip()):
            self.db.upsert_coin(mint=text.strip())
            return [self.db.coin(text.strip())], None
        return [], "Unknown name. Send the mint."

    def _schedule_audit(self, mint: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.audit_now(mint))

    def _buy(self, text: str) -> list[dict]:
        matches, error = self._resolve(text)
        if error:
            return [{"text": error}]
        if len(matches) > 1:
            return [{"text": self._which(matches)}]
        coin = matches[0]
        mint = coin["mint"]
        since = self.settings.get("desk.night_start") or "1970"
        signal = self.db.signal_tonight(mint, since)
        link = coin_url(mint, "solana")
        if signal:
            mode = self.settings.get("desk.mode")
            prefix = "PAPER · " if mode == "PAPER" else ""
            body = f"{prefix}Buy {coin['name'] or mint} · ${float(signal['size_usd'] or 0):.2f}\n{link}"
            return [{"text": body, "keyboard": [[{"text": "Buy on FOMO", "url": link}]]}]
        self._schedule_audit(mint)
        layers = self.db.audits_for(mint)
        lines = [coin["name"] or mint]
        if layers:
            for row in layers:
                lines.append(f"{row['layer']} {row['name']}: {row['value_text']}")
        else:
            lines.append("Taking it apart.")
        lines.append("not sized, not on the desk")
        lines.append(link)
        return [{"text": "\n".join(lines), "keyboard": [[{"text": "Buy on FOMO", "url": link}]]}]

    def _sell(self, text: str) -> list[dict]:
        matches, error = self._resolve(text)
        if error:
            return [{"text": error}]
        if len(matches) > 1:
            return [{"text": self._which(matches)}]
        coin = matches[0]
        row = self.db.open_position_for(coin["mint"])
        link = coin_url(coin["mint"], "solana")
        if not row:
            return [{"text": f"No open position.\n{link}"}]
        return [
            {
                "text": f"Sell {coin['name'] or coin['mint']}\n{link}",
                "keyboard": [
                    [{"text": "Sell on FOMO", "url": link}],
                    [{"text": "Sold", "callback_data": f"sold:{coin['mint']}"}],
                ],
            }
        ]

    def _why(self, text: str) -> list[dict]:
        matches, error = self._resolve(text)
        if error:
            return [{"text": error}]
        if len(matches) > 1:
            return [{"text": self._which(matches)}]
        coin = matches[0]
        mint = coin["mint"]
        link = coin_url(mint, "solana")
        audits = self.db.audits_for(mint)
        reds = [row for row in audits if row["pass"] == 0 and row["value_text"] != "not checked"]
        if reds:
            row = reds[0]
            return [{"text": f"Auditor stopped {coin['name'] or mint}. Layer {row['layer']} {row['value_text']} at {row['ts']}.\n{link}"}]
        verdicts = self.db.verdicts_since("1970")
        for row in verdicts:
            if row["mint"] == mint and not row["pass"]:
                return [{"text": f"Analyst stopped {coin['name'] or mint}. {row['reason']} at {row['ts']}.\n{link}"}]
        scans = self.db.scans_since("1970")
        for row in scans:
            if row["mint"] == mint and row["verdict"] == "kill":
                return [{"text": f"Scanner stopped {coin['name'] or mint}. {row['reason']} at {row['ts']}.\n{link}"}]
        positions = self.db.positions_since("1970")
        for row in positions:
            if row["mint"] == mint and row["exit_reason"]:
                return [{"text": f"Risk stopped {coin['name'] or mint}. {row['exit_reason']} at {row['closed_at']}.\n{link}"}]
        return [{"text": f"Nothing stopped {coin['name'] or mint}.\n{link}"}]

    def _which(self, matches) -> str:
        lines = ["Which coin?"]
        for row in matches:
            lines.append(f"{row['name']} {row['symbol']} {coin_url(row['mint'], 'solana')}")
        return "\n".join(lines)

    def _status(self) -> dict:
        mode = self.settings.get("desk.mode")
        equity = _equity(self.db, self.settings)
        lines = [f"{mode}", f"Balance ${equity:.2f}"]
        keyboard = []
        for row in self.db.open_positions():
            link = coin_url(row["mint"], "solana")
            lines.append(f"{row['name'] or row['mint']} ${row['size_usd']:.2f} {link}")
            keyboard.append([{"text": "Sell on FOMO", "url": link}])
        if not keyboard:
            lines.append("No open positions")
        return {"text": "\n".join(lines), "keyboard": keyboard or None}

    def _report_line(self) -> str:
        url = os.environ.get("PAGES_URL") or ""
        if url:
            return url.rstrip("/") + "/morning.html"
        return "Morning page is written by report.py. No public link is set."

    async def audit_now(self, mint: str) -> None:
        """buy on an unaudited mint runs the auditor and does not size the coin."""
        coin = self.db.coin(mint)
        message = {
            "mint": mint,
            "name": coin["name"] if coin else mint,
            "symbol": coin["symbol"] if coin else None,
            "pool": coin["pool"] if coin else None,
            "liquidityUsd": None,
            "dex": {},
        }
        # The auditor task is the one that fetches. Queue it without the analyst handoff
        # by using a direct call when the owner asks.
        from agents.auditor import Auditor

        auditor = Auditor(self.db, self.settings, self.bus)
        result = await auditor.handle(message, enqueue=False)
        if result.get("reason") == "already rejected":
            return
        link = coin_url(mint, "solana")
        lines = [f"{message['name']}"]
        for layer in result.get("layers") or []:
            mark = "ok" if layer["pass"] is True else ("red" if layer["pass"] is False else "grey")
            lines.append(f"{layer['n']} {layer['name']}: {layer['value']} ({mark})")
        lines.append("not sized, not on the desk")
        lines.append(link)
        await self.bus.events.put({"type": "text", "text": "\n".join(lines)})


def _mintish(text: str) -> bool:
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return 32 <= len(text) <= 44 and all(ch in alphabet for ch in text)


def _equity(db, settings) -> float:
    start = float(settings.get("desk.night_start_equity") or settings.get("desk.paper_start_stack"))
    since = settings.get("desk.night_start") or "1970"
    total = start
    for row in db.positions_since(since):
        if row["status"] == "closed" and row["pnl_usd"] is not None:
            total += float(row["pnl_usd"])
        elif row["status"] in ("open", "exiting"):
            last = db.latest_price(row["mint"])
            if last and row["entry_price"]:
                mark = float(row["size_usd"]) * (float(last["price"]) / float(row["entry_price"]))
                total += mark - float(row["size_usd"])
    return total
