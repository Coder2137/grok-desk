"""Plan B JSON writer. Plaintext files land only under a secret directory from the environment.

The same bytes are encrypted into the live root for the public phone site.
Nothing in this module reads a token out of the repository. Bare /d/ is not created.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import GROUPS, SETTINGS, iso, parse_iso, utc_now
from fields import dex_fields, pumpswap_pair
from links import coin_url, profile_url
from livecrypto import encrypt_envelope, envelope_bytes, is_live_path
from logutil import file_logger, log
from sources import DexRateLimit, dexscreener_batch
from telegram import _equity

LOG = file_logger("api", "api.log")

app = FastAPI()


class Health(BaseModel):
    ok: bool


def _token_ok(authorization: str | None) -> bool:
    expected = os.environ.get("APP_TOKEN") or ""
    if not expected:
        return False
    return authorization == f"Bearer {expected}"


@app.get("/api/health")
def health(authorization: str | None = Header(default=None)) -> Health:
    if not _token_ok(authorization):
        raise HTTPException(status_code=401)
    return Health(ok=True)


def _data_path() -> str | None:
    data_path = os.environ.get("DATA_PATH") or ""
    if len(data_path) < 32 or "/" in data_path or "\\" in data_path or data_path in (".", ".."):
        return None
    return data_path


def json_root() -> Path | None:
    data_path = _data_path()
    if data_path is None:
        return None
    base = Path(os.environ.get("JSON_ROOT") or "var/json")
    return base / "d" / data_path


def live_root() -> Path:
    return Path(os.environ.get("LIVE_ROOT") or "var/live")


PLAIN_HASHES = ".plain-hashes.json"


def _under(root: Path, relative: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root
    for part in relative.split("/"):
        path = path / part
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("path")
    return path


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _plain_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash_ok(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def load_plain_hashes() -> dict[str, str]:
    path = live_root() / PLAIN_HASHES
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found = {}
    for key, value in payload.items():
        if isinstance(key, str) and is_live_path(key) and _hash_ok(value):
            found[key] = value
    return found


def save_plain_hashes(hashes: dict[str, str]) -> None:
    clean = {key: value for key, value in sorted(hashes.items()) if is_live_path(key) and _hash_ok(value)}
    _write_bytes(live_root() / PLAIN_HASHES, json.dumps(clean, separators=(",", ":")).encode("utf-8"))


def _emit(relative: str, payload, plain_root: Path, secret: str, hashes: dict[str, str]) -> str | None:
    if not is_live_path(relative):
        return None
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    digest = _plain_digest(raw)
    plain_path = _under(plain_root, relative)
    live_path = _under(live_root(), relative)
    if hashes.get(relative) == digest and plain_path.is_file() and live_path.is_file():
        return relative
    envelope = envelope_bytes(encrypt_envelope(raw, secret, relative))
    _write_bytes(plain_path, raw)
    _write_bytes(live_path, envelope)
    hashes[relative] = digest
    return relative


def _public_rpc(url: str) -> str:
    if not url or "?" in url or "@" in url:
        return "custom"
    return url


def _age_label(migrated: str | None, now) -> str:
    if not migrated:
        return "age n/a"
    minutes = (now - parse_iso(migrated)).total_seconds() / 60.0
    if minutes < 60:
        return f"{int(minutes)}m old"
    if minutes < 60 * 48:
        return f"{int(minutes // 60)}h old"
    return f"{int(minutes // 1440)}d old"


def _candles(rows) -> list[dict]:
    buckets: dict[str, list] = {}
    previous_volume = None
    for row in rows:
        minute = row["ts"][:16]
        volume = row["volume_m5"]
        delta = None
        if volume is not None and previous_volume is not None:
            delta = float(volume) - float(previous_volume)
        if volume is not None:
            previous_volume = float(volume)
        buckets.setdefault(minute, []).append((float(row["price"]), delta))
    candles = []
    for minute, pairs in buckets.items():
        prices = [item[0] for item in pairs]
        volumes = [item[1] for item in pairs if item[1] is not None]
        candles.append(
            {
                "t": minute,
                "o": prices[0],
                "h": max(prices),
                "l": min(prices),
                "c": prices[-1],
                "v": volumes[-1] if volumes else 0,
            }
        )
    return candles


def build_state(db, settings) -> dict:
    now = utc_now()
    equity = _equity(db, settings)
    start = float(settings.get("desk.night_start_equity") or equity or 1)
    change = ((equity - start) / start * 100) if start else 0
    multiple = (equity / start) if start else 1
    beats = db.latest_heartbeats()
    since = settings.get("desk.night_start") or "1970"
    scans = db.scans_since(since)
    audits_rejected = {}
    taken = set()
    for row in db.activity_since(since, 200):
        if row["agent"] == "Auditor" and row["kind"] == "rejected":
            audits_rejected[row["mint"]] = row
        if row["kind"] == "rejected" and row["agent"] == "Auditor":
            taken.add(row["mint"])
    for mint in db.audited_mints(since):
        taken.add(mint)
    positions = db.open_positions()
    activity = [_activity(row) for row in db.latest_activity(20)]
    handle = settings.get("desk.fomo_handle") or ""
    return {
        "ts": iso(now),
        "mode": settings.get("desk.mode"),
        "paper": settings.get("desk.mode") == "PAPER",
        "balance": round(equity, 2),
        "nightStartBalance": round(start, 2),
        "dayChangePct": round(change, 2),
        "multiple": round(multiple, 2),
        "stack": settings.get("desk.stack"),
        "stackPct": settings.get("executor.stack_pct"),
        "stopPct": settings.get("risk.stop_pct"),
        "trailPct": settings.get("risk.trail_pct"),
        "trailStartPct": settings.get("risk.trail_start_pct"),
        "auditorEnabled": bool(settings.get("auditor.enabled")),
        "telegram": bool(settings.get("desk.owner_chat_id")),
        "fomoHandle": handle,
        "profileUrl": profile_url(handle) if handle else "",
        "ticketNote": settings.ticket_note() or "",
        "layer8Down": bool(settings.get("desk.layer8_down")),
        "heartbeats": beats,
        "agents": {
            "scanner": _scanner_card(db, scans, beats, settings),
            "auditor": _auditor_card(db, since, beats, settings),
            "analyst": _analyst_card(db, since, beats, settings),
            "executor": _executor_card(db, since, beats, settings),
            "risk": _risk_card(db, since, beats, settings, bool(positions)),
        },
        "activity": activity[:5],
        "activityMore": activity,
        "coins": [
            {"mint": row["mint"], "name": row["name"], "symbol": row["symbol"]}
            for row in db.coins()
        ],
        "positions": [_position(row) for row in positions],
        "settings": _settings_snapshot(settings),
    }


def _activity(row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "mint": row["mint"],
        "agent": row["agent"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "value": row["value"],
    }


def _position(row) -> dict:
    return {
        "mint": row["mint"],
        "name": row["name"],
        "entry": row["entry_price"],
        "size": row["size_usd"],
        "peak": row["peak_price"],
        "stop": row["stop_price"],
        "stopPct": row["stop_pct"],
        "trailPct": row["trail_pct"],
        "trailActive": bool(row["trail_active"]),
        "status": row["status"],
        "mode": row["mode"],
        "link": coin_url(row["mint"], "solana"),
    }


def _alive(beats, agent, settings) -> bool:
    moment = beats.get(agent)
    if not moment:
        return False
    age = (utc_now() - parse_iso(moment)).total_seconds()
    return age < 180


def _scanner_card(db, scans, beats, settings) -> dict:
    rows = []
    for scan in list(scans)[:20]:
        coin = db.coin(scan["mint"])
        name = coin["name"] if coin and coin["name"] else scan["mint"]
        rows.append(
            {
                "mint": scan["mint"],
                "name": name,
                "verdict": scan["verdict"],
                "reason": scan["reason"],
                "liquidity": scan["liquidity_usd"],
            }
        )
    passed = sum(1 for row in scans if row["verdict"] == "pass")
    return {
        "paused": bool(settings.get("agent.scanner.paused")),
        "running": _alive(beats, "scanner", settings) and not settings.get("agent.scanner.paused"),
        "count": len(scans),
        "passed": passed,
        "rows": rows,
    }


def _auditor_card(db, since, beats, settings) -> dict:
    mints = db.audited_mints(since)
    rejected = 0
    current = None
    for mint in mints:
        layers = db.audits_for(mint)
        red = any(row["pass"] == 0 and row["value_text"] != "not checked" for row in layers)
        if red:
            rejected += 1
        current = mint
    return {
        "paused": bool(settings.get("agent.auditor.paused")),
        "running": _alive(beats, "auditor", settings) and not settings.get("agent.auditor.paused"),
        "count": len(mints),
        "rejected": rejected,
        "current": current,
        "enabled": bool(settings.get("auditor.enabled")),
    }


def _analyst_card(db, since, beats, settings) -> dict:
    rows = []
    for row in db.verdicts_since(since)[:20]:
        rows.append(
            {
                "mint": row["mint"],
                "score": row["score"],
                "passed": bool(row["pass"]),
                "reason": row["reason"],
                "model": "rules" if row["input_tokens"] == 0 else "jev",
            }
        )
    return {
        "paused": bool(settings.get("agent.analyst.paused")),
        "running": _alive(beats, "analyst", settings) and not settings.get("agent.analyst.paused"),
        "count": len(rows),
        "rows": rows,
        "lastScore": rows[0]["score"] if rows else None,
    }


def _executor_card(db, since, beats, settings) -> dict:
    signals = [row for row in db.signals_since(since) if row["status"] == "bought"]
    last = signals[0] if signals else None
    return {
        "paused": bool(settings.get("agent.executor.paused")),
        "running": _alive(beats, "executor", settings) and not settings.get("agent.executor.paused"),
        "count": len(signals),
        "last": None
        if not last
        else {
            "mint": last["mint"],
            "size": last["size_usd"],
            "ts": last["ts"],
            "mode": last["mode"],
        },
    }


def _risk_card(db, since, beats, settings, locked: bool) -> dict:
    exits = []
    for row in db.positions_since(since):
        if row["exit_reason"]:
            exits.append({"mint": row["mint"], "reason": row["exit_reason"], "pnl": row["pnl_usd"], "ts": row["closed_at"]})
    return {
        "paused": bool(settings.get("agent.risk.paused")),
        "running": (not settings.get("agent.risk.paused")) and (_alive(beats, "risk", settings) or locked),
        "locked": locked,
        "count": len(exits),
        "last": exits[-1] if exits else None,
        "stopPct": settings.get("risk.stop_pct"),
        "trailPct": settings.get("risk.trail_pct"),
    }


def _settings_snapshot(settings) -> dict:
    grouped: dict[str, list] = {}
    for key, spec in SETTINGS.items():
        if key.startswith("desk.owner") or key.startswith("desk.night") or key.startswith("desk.paper_nights") or key.startswith("desk.layer8") or key.startswith("agent."):
            continue
        group = key.split(".", 1)[0]
        value = settings.get(key)
        if key == "desk.rpc_url":
            value = _public_rpc(str(value))
        grouped.setdefault(GROUPS.get(group, group), []).append(
            {
                "key": key,
                "value": value,
                "kind": spec[0],
                "min": spec[2] if spec[0] in ("int", "float") else None,
                "max": spec[3] if spec[0] in ("int", "float") else None,
            }
        )
    return grouped


def build_chart(db, settings) -> dict:
    since = settings.get("desk.night_start") or "1970"
    points = [{"t": row["ts"], "v": row["value"]} for row in db.balances_since(since)]
    marks = []
    for row in db.activity_since(since, 100):
        if row["kind"] == "bought":
            marks.append({"t": row["ts"], "kind": "buy", "text": row["title"], "mint": row["mint"]})
        elif row["kind"] == "sold":
            marks.append({"t": row["ts"], "kind": "up", "text": row["value"], "mint": row["mint"]})
        elif row["kind"] == "stopped":
            marks.append({"t": row["ts"], "kind": "down", "text": f"{row['subtitle']} · {row['value']}", "mint": row["mint"]})
    return {
        "startValue": float(settings.get("desk.night_start_equity") or 0),
        "nightStart": since,
        "points": points,
        "marks": list(reversed(marks)),
    }


def build_activity(db) -> dict:
    return {"rows": [_activity(row) for row in db.latest_activity(100)]}


def build_coin(db, settings, mint: str, market: dict | None = None) -> dict:
    coin = db.coin(mint)
    now = utc_now()
    layers = []
    for row in db.audits_for(mint):
        layers.append(
            {
                "n": row["layer"],
                "name": row["name"],
                "pass": None if row["pass"] is None else bool(row["pass"]),
                "value": row["value_text"],
                "source": row["source"],
            }
        )
    checked = [layer for layer in layers if layer["value"] != "not checked"]
    passed = bool(layers) and all(layer["pass"] for layer in layers)
    shield = f"{sum(1 for layer in layers if layer['pass'])}/8" if layers else None
    price_row = db.latest_price(mint)
    position = db.open_position_for(mint)
    closed = None
    if position is None:
        for row in db.positions_since("1970"):
            if row["mint"] == mint:
                closed = row
    shown = position or closed
    entry = float(shown["entry_price"]) if shown else None
    price = float(price_row["price"]) if price_row else None
    change = None
    if entry and price:
        change = (price / entry - 1) * 100
    risk_line = None
    if shown and shown["status"] in ("open", "exiting"):
        hard = float(shown["stop_price"])
        if shown["trail_active"]:
            trail = float(shown["peak_price"]) * (1 - float(shown["trail_pct"]) / 100.0)
            risk_line = max(hard, trail)
        else:
            risk_line = hard
    link = coin_url(mint, "solana")
    holders = coin["holders"] if coin else None
    market = market or {}
    scan_row = db.latest_scan(mint)
    scan = None
    if scan_row:
        scan = {
            "verdict": scan_row["verdict"],
            "reason": scan_row["reason"],
            "liquidity": scan_row["liquidity_usd"],
            "top10": scan_row["top10_pct"],
            "h1": scan_row["h1_change_pct"],
            "ageMin": scan_row["age_min"],
            "ts": scan_row["ts"],
        }
    return {
        "mint": mint,
        "name": coin["name"] if coin else mint,
        "symbol": coin["symbol"] if coin else "",
        "chain": "Solana",
        "age": _age_label(coin["migrated_at"] if coin else None, now),
        "shield": shield,
        "passedAudit": passed,
        "layersChecked": len(checked),
        "price": price,
        "entryPrice": entry,
        "changeSinceEntry": change,
        "candles": _candles(db.prices_for(mint)),
        "riskLine": risk_line,
        "marketCap": market.get("marketCap"),
        "liquidity": market.get("liquidityUsd"),
        "volume24": market.get("volumeH24"),
        "holders": holders,
        "scan": scan,
        "position": None if not shown else _position(shown),
        "layers": layers,
        "link": link,
        "mode": settings.get("desk.mode"),
    }


class Writer:
    def __init__(self, db, settings, bus) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self._dex_at = 0.0

    async def run(self) -> None:
        while True:
            try:
                self.write_all()
                loop = asyncio.get_running_loop()
                if loop.time() - self._dex_at >= 60:
                    await self.cover_prices()
                    self._dex_at = loop.time()
            except Exception as exc:
                log(LOG, f"writer {exc.__class__.__name__}")
            self.db.heartbeat("api")
            await asyncio.sleep(15)

    def write_all(self) -> None:
        root = json_root()
        secret = _data_path()
        if root is None or secret is None:
            return
        equity = _equity(self.db, self.settings)
        self.db.add_balance(equity)
        hashes = load_plain_hashes()
        before = dict(hashes)
        kept: set[str] = set()
        snapshots = (
            ("state.json", build_state(self.db, self.settings)),
            ("chart.json", build_chart(self.db, self.settings)),
            ("activity.json", build_activity(self.db)),
        )
        for relative, payload in snapshots:
            name = _emit(relative, payload, root, secret, hashes)
            if name:
                kept.add(name)
        for mint in self._screen_mints():
            name = _emit(
                f"coin/{mint}.json",
                build_coin(self.db, self.settings, mint, self.bus.market.get(mint)),
                root,
                secret,
                hashes,
            )
            if name:
                kept.add(name)
        for key in list(hashes):
            if key not in kept:
                hashes.pop(key, None)
        if hashes != before:
            save_plain_hashes(hashes)

    def _screen_mints(self) -> list[str]:
        since = self.settings.get("desk.night_start") or "1970"
        mints = set(self.db.audited_mints(since))
        mints.update(self.db.pushed_mints(since))
        for row in self.db.open_positions():
            mints.add(row["mint"])
        for row in self.db.coins():
            if row["mint"]:
                mints.add(row["mint"])
        return sorted(mints)

    async def cover_prices(self) -> None:
        since = self.settings.get("desk.night_start") or "1970"
        covered = set(self.bus.waiting) | set(self.bus.open_mints)
        wanted = [
            mint
            for mint in set(self.db.audited_mints(since)) | set(self.db.pushed_mints(since))
            if mint not in covered
        ]
        if not wanted:
            return
        try:
            async with httpx.AsyncClient() as client:
                payload = await dexscreener_batch(wanted, client)
        except DexRateLimit:
            log(LOG, "dexscreener 429")
            return
        except Exception as exc:
            log(LOG, f"dex {exc.__class__.__name__}")
            return
        grouped: dict[str, list] = {}
        for row in payload:
            if isinstance(row, dict):
                mint = ((row.get("baseToken") or {}).get("address"))
                if mint:
                    grouped.setdefault(mint, []).append(row)
        moment = iso(utc_now())
        for mint, rows in grouped.items():
            pair = pumpswap_pair(rows)
            if not pair:
                continue
            fields = dex_fields(pair)
            self.bus.market[mint] = fields
            if fields.get("priceUsd") is not None:
                self.db.add_price(mint, fields["priceUsd"], fields.get("volumeM5"), moment)
