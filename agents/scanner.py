"""Scanner. Every coin that graduates off pump.fun onto PumpSwap. Nothing else."""

import asyncio
import json
from pathlib import Path

import httpx
import websockets

from config import iso, parse_iso, utc_now
from fields import dex_fields, migration_frame, pumpswap_pair
from logutil import file_logger, log
from sources import RUG_REPORT, DexRateLimit, dexscreener_batch

LOG = file_logger("scanner", "scanner.log")
WS_URL = "wss://pumpportal.fun/api/data"
SUBSCRIBE_MIGRATION = {"method": "subscribeMigration"}
# Reply observed from that subscribe call. It is not a migration frame.
SUBSCRIPTION_ACK = "Subscribed to 'migration' events."
RAW_DIR = Path("logs") / "raw"


def subscription_ack(message) -> bool:
    """True only for the subscribeMigration acknowledgement, not a coin frame."""
    return isinstance(message, dict) and message.get("message") == SUBSCRIPTION_ACK


def _sample_is_ack(path: Path) -> bool:
    try:
        return subscription_ack(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def normalize_label(value: str) -> str:
    """Lowercase, drop spaces, digits, and emoji. Letters only."""
    if not value:
        return ""
    return "".join(ch.lower() for ch in value if ch.isalpha())


def fmt_liq(value: float) -> str:
    number = float(value)
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if number >= 1000:
        return f"${number / 1000:.1f}K"
    return f"${number:.0f}"


def fmt_pct(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 0.05:
        return str(int(round(number)))
    return f"{number:.1f}"


def copycat_of(name: str, symbol: str, runners: list[dict]) -> str | None:
    labels = [normalize_label(name), normalize_label(symbol)]
    labels = [item for item in labels if item]
    for runner in runners:
        others = [normalize_label(runner.get("name") or ""), normalize_label(runner.get("symbol") or "")]
        others = [item for item in others if item]
        for left in labels:
            for right in others:
                if left == right or left in right or right in left:
                    return runner.get("symbol") or runner.get("name") or "runner"
    return None


def top10_pct(accounts: list[dict], supply: float, pool: str | None, curve: str | None) -> float | None:
    """Share of supply held by the ten largest accounts.

    Accounts whose address or owner equals the pool or the bonding curve are
    left out. That drops the PumpSwap LP token account, whose owner is the
    pool from DexScreener. Burn addresses are not removed: this desk has no
    burn list, and the holder feed does not label them.
    """
    if supply is None or supply <= 0 or accounts is None:
        return None
    kept = []
    for row in accounts:
        owner = row.get("owner")
        address = row.get("address")
        if pool and (owner == pool or address == pool):
            continue
        if curve and (owner == curve or address == curve):
            continue
        if row.get("balance") is None:
            continue
        kept.append(float(row["balance"]))
    kept.sort(reverse=True)
    return sum(kept[:10]) / float(supply) * 100.0


def holder_rows(top_holders) -> list[dict] | None:
    """RugCheck topHolders rows. `pct` is already a percent of supply.

    A missing list, an empty list, or rows with no pct stay missing. Nothing
    here fills a gap with zero.
    """
    if not isinstance(top_holders, list) or not top_holders:
        return None
    rows = []
    for item in top_holders:
        if not isinstance(item, dict) or "pct" not in item or item.get("pct") is None:
            continue
        try:
            pct = float(item["pct"])
        except (TypeError, ValueError):
            continue
        owner = item.get("owner")
        address = item.get("address")
        rows.append(
            {
                "owner": None if owner is None else str(owner),
                "address": None if address is None else str(address),
                "balance": pct,
            }
        )
    return rows or None


def top10_from_holders(top_holders, pool: str | None, curve: str | None) -> float | None:
    rows = holder_rows(top_holders)
    if rows is None:
        return None
    return top10_pct(rows, 100.0, pool, curve)


def _top10_result(fetched) -> tuple[float | None, str | None]:
    if isinstance(fetched, tuple) and len(fetched) == 2:
        pct, cause = fetched
        return pct, None if cause is None else str(cause)
    return fetched, None


def judge(metrics: dict, settings: dict, runners: list[dict]) -> tuple[str, str | None]:
    """First kill wins. Pass only when the pool is old enough and DexScreener knows it."""
    if not metrics.get("known"):
        age = metrics.get("age_min")
        if age is not None and age > float(settings["scanner.max_wait_min"]):
            return "kill", "never qualified"
        return "wait", None
    liq = metrics.get("liquidity")
    if liq is None:
        return "wait", None
    if liq < float(settings["scanner.min_liquidity_usd"]):
        return "kill", f"liq {fmt_liq(liq)}"
    top = metrics.get("top10")
    if top is not None and top > float(settings["scanner.max_top10_pct"]):
        return "kill", f"top 10 hold {fmt_pct(top)}%"
    h1 = metrics.get("h1")
    if h1 is not None and h1 > float(settings["scanner.max_h1_change_pct"]):
        return "kill", f"+{fmt_pct(h1)}% in 1h"
    copied = copycat_of(metrics.get("name") or "", metrics.get("symbol") or "", runners)
    if copied:
        return "kill", f"copycat of {copied}"
    age = metrics.get("age_min")
    if top is None:
        if age is not None and age > float(settings["scanner.max_wait_min"]):
            cause = metrics.get("top10_error")
            if cause:
                return "kill", f"top10 unavailable: {cause}"
            return "kill", "never qualified"
        return "wait", None
    if age is None:
        return "wait", None
    if age > float(settings["scanner.max_wait_min"]):
        return "kill", "never qualified"
    if age < float(settings["scanner.min_pool_age_min"]):
        return "wait", None
    return "pass", None


class Scanner:
    def __init__(self, db, settings, bus, source: bool = True) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.source = source
        self.waiting: dict[str, dict] = {}
        self._sample_written = False
        self._ack_sample_noted = False

    async def run(self) -> None:
        if self.source:
            await asyncio.gather(self._socket(), self._poll())
        else:
            await asyncio.gather(self._follow(), self._poll())

    async def _socket(self) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, open_timeout=20) as socket:
                    await socket.send(json.dumps(SUBSCRIBE_MIGRATION))
                    backoff = 1
                    while True:
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=120)
                        except asyncio.TimeoutError:
                            log(LOG, f"{iso(utc_now())} websocket silent 120s reconnect")
                            break
                        self.ingest_message(raw, relay=True)
            except Exception as exc:
                log(LOG, f"{iso(utc_now())} websocket closed {exc.__class__.__name__} backoff {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
            self.db.heartbeat("scanner")

    async def _follow(self) -> None:
        from feed import load_offset

        offset = load_offset()
        while True:
            try:
                offset = self.follow_once(offset)
            except Exception as exc:
                log(LOG, f"{iso(utc_now())} feed {exc.__class__.__name__}")
            self.db.heartbeat("scanner")
            await asyncio.sleep(0.5)

    def follow_once(self, offset: int | None = None) -> int:
        from feed import load_offset, read_new, store_offset

        if offset is None:
            offset = load_offset()
        lines, offset = read_new(offset)
        for line in lines:
            self.ingest_message(line, relay=False)
        store_offset(offset)
        return offset

    def ingest_message(self, raw, relay: bool = False) -> None:
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return
        if isinstance(message, dict) and message.get("method"):
            return
        if subscription_ack(message):
            log(LOG, f"{iso(utc_now())} migration subscription acknowledged")
            if not self._ack_sample_noted and _sample_is_ack(RAW_DIR / "migration-sample.json"):
                self._ack_sample_noted = True
                log(LOG, f"{iso(utc_now())} saved migration sample is a subscription ack")
            return
        fields = migration_frame(message)
        if fields is None:
            return
        if relay:
            from feed import append_migration

            append_migration(message)
        self._save_sample(message)
        if not fields["mint"]:
            log(LOG, f"{iso(utc_now())} migration frame mint unset")
            return
        self.admit(
            {
                "mint": fields["mint"],
                "pool": None,
                "pool_label": fields["pool"],
                "signature": fields["signature"],
                "creator": fields["creator"],
                "migratedAt": iso(utc_now()),
            }
        )

    def _save_sample(self, message) -> None:
        if self._sample_written or subscription_ack(message):
            return
        try:
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            path = RAW_DIR / "migration-sample.json"
            if path.exists() and not _sample_is_ack(path):
                self._sample_written = True
                return
            path.write_text(json.dumps(message, default=str)[:20000], encoding="utf-8")
            self._sample_written = True
        except OSError:
            return

    def admit(self, coin: dict) -> None:
        mint = coin["mint"]
        since = self.settings.get("desk.night_start") or "1970"
        if self.db.passed_tonight(mint, since) or mint in self.waiting:
            return
        self.db.upsert_coin(
            mint=mint,
            pool=None,
            migrated_at=coin.get("migratedAt"),
            creator=coin.get("creator"),
            creator_balance_at_migration=coin.get("creatorBalance"),
        )
        self.waiting[mint] = coin
        self.bus.waiting.add(mint)

    async def _poll(self) -> None:
        while True:
            try:
                if not self.settings.get("agent.scanner.paused"):
                    await self.poll_once()
            except DexRateLimit:
                log(LOG, f"{iso(utc_now())} dexscreener 429 wait 30s")
                await asyncio.sleep(30)
                continue
            except Exception as exc:
                log(LOG, f"{iso(utc_now())} poll {exc.__class__.__name__}")
            self.db.heartbeat("scanner")
            await asyncio.sleep(int(self.settings.get("scanner.poll_s")))

    async def poll_once(self, pairs=None) -> None:
        if not self.waiting:
            return
        mints = list(self.waiting)
        if pairs is None:
            async with httpx.AsyncClient() as client:
                payload = await dexscreener_batch(mints, client)
        else:
            payload = pairs
        by_mint: dict[str, dict] = {}
        if isinstance(payload, list):
            grouped: dict[str, list] = {}
            for row in payload:
                if isinstance(row, dict):
                    mint = ((row.get("baseToken") or {}).get("address"))
                    if mint:
                        grouped.setdefault(mint, []).append(row)
            for mint, rows in grouped.items():
                pair = pumpswap_pair(rows)
                if pair:
                    by_mint[mint] = dex_fields(pair)
        settings = self.settings.all()
        day = utc_now().date().isoformat()
        runners = [dict(row) for row in self.db.runners_for(day)]
        now = utc_now()
        for mint in list(self.waiting):
            coin = self.waiting[mint]
            dex = by_mint.get(mint)
            known = dex is not None
            if known and dex.get("priceUsd") is not None:
                self.db.add_price(mint, dex["priceUsd"], dex.get("volumeM5"), iso(now))
                self.bus.market[mint] = dex
            if known and (dex.get("priceChangeH24") or 0) >= float(settings["scanner.runner_h24_pct"]):
                self.db.add_runner(mint, dex.get("name"), dex.get("symbol"), day, dex.get("priceChangeH24"))
                runners.append({"name": dex.get("name"), "symbol": dex.get("symbol"), "mint": mint})
            migrated = coin.get("migratedAt")
            age = None
            if migrated:
                age = (now - parse_iso(migrated)).total_seconds() / 60.0
            if known and coin.get("top10") is None:
                pct, cause = _top10_result(
                    await self._top10(mint, (dex or {}).get("pool") or coin.get("pool"), coin.get("curve"))
                )
                if pct is None:
                    if cause:
                        coin["top10_error"] = cause
                        log(LOG, f"{iso(now)} {mint} top10 unavailable: {cause}")
                else:
                    coin["top10"] = pct
                    coin.pop("top10_error", None)
            metrics = {
                "known": known,
                "liquidity": None if not dex else dex.get("liquidityUsd"),
                "top10": coin.get("top10"),
                "top10_error": coin.get("top10_error"),
                "h1": None if not dex else dex.get("priceChangeH1"),
                "name": None if not dex else dex.get("name"),
                "symbol": None if not dex else dex.get("symbol"),
                "age_min": age,
            }
            verdict, reason = judge(metrics, settings, runners)
            if verdict == "wait":
                if known:
                    self._remember_names(mint, dex)
                continue
            self._emit(mint, coin, dex, metrics, verdict, reason, now)
            self.waiting.pop(mint, None)
            self.bus.waiting.discard(mint)

    async def _fetch_report(self, mint: str) -> tuple[dict | None, str | None]:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(RUG_REPORT.format(mint=mint), timeout=15)
        except Exception as exc:
            return None, exc.__class__.__name__
        if response.status_code >= 400:
            return None, f"rugcheck http {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return None, "rugcheck report"
        if not isinstance(body, dict):
            return None, "rugcheck report"
        return body, None

    async def _top10(self, mint: str, pool: str | None, curve: str | None):
        """Holder concentration from RugCheck topHolders.

        The public Solana RPC answers getTokenLargestAccounts with HTTP 429,
        and that used to come back as a silent null. RugCheck's report needs
        no key and already lists each holder's percent of supply. Pool and
        curve accounts are excluded by top10_pct. A failed read stays null
        and names the cause so a later poll inside scanner.max_wait_min can
        try again.
        """
        body, cause = await self._fetch_report(mint)
        if cause or not isinstance(body, dict):
            return None, cause or "rugcheck report"
        if not isinstance(body.get("topHolders"), list) or not body.get("topHolders"):
            return None, "topHolders missing"
        pct = top10_from_holders(body.get("topHolders"), pool, curve)
        if pct is None:
            return None, "topHolders missing"
        return pct, None

    def _remember_names(self, mint, dex) -> None:
        if dex.get("name") or dex.get("symbol"):
            self.db.upsert_coin(mint=mint, name=dex.get("name"), symbol=dex.get("symbol"), pool=dex.get("pool"))

    def _emit(self, mint, coin, dex, metrics, verdict, reason, now) -> None:
        name = (dex or {}).get("name") or coin.get("name")
        symbol = (dex or {}).get("symbol") or coin.get("symbol")
        pool = (dex or {}).get("pool") or coin.get("pool")
        self.db.upsert_coin(mint=mint, name=name, symbol=symbol, pool=pool)
        row = {
            "agent": "scanner",
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "pool": pool,
            "liquidityUsd": metrics.get("liquidity"),
            "top10Pct": metrics.get("top10"),
            "h1ChangePct": metrics.get("h1"),
            "ageMin": metrics.get("age_min"),
            "verdict": verdict,
            "reason": reason,
            "ts": iso(now),
        }
        self.db.add_scan(
            {
                "mint": mint,
                "ts": row["ts"],
                "liquidity_usd": row["liquidityUsd"],
                "top10_pct": row["top10Pct"],
                "h1_change_pct": row["h1ChangePct"],
                "age_min": row["ageMin"],
                "verdict": verdict,
                "reason": reason,
            }
        )
        log(
            LOG,
            f"{row['ts']} {mint} {verdict} {reason} liq {row['liquidityUsd']} top10 {row['top10Pct']} h1 {row['h1ChangePct']} age {row['ageMin']}",
        )
        if verdict == "pass":
            payload = dict(row)
            payload["dex"] = dex or {}
            payload["migratedAt"] = coin.get("migratedAt")
            payload["creator"] = coin.get("creator")
            asyncio.create_task(self.bus.auditor.put(payload))
