"""Analyst. Rules or Jev produce four nouls. Only the gate decides."""

import asyncio

import httpx

from config import iso, utc_now
from logutil import file_logger, log

LOG = file_logger("analyst", "analyst.log")
JEV_URL = "https://api.typesafe.ai/v1/systemone"

QUESTIONS = (
    {
        "name": "alive_6h",
        "type": "noul",
        "instruction": "Chance this coin is still trading with real volume 6 hours from now. Reply with a noul from 0 to 1.",
    },
    {
        "name": "organic",
        "type": "noul",
        "instruction": "How organic the buying looks versus bundled wallets. Reply with a noul from 0 to 1.",
    },
    {
        "name": "flush",
        "type": "noul",
        "instruction": "Whether the last 5 minutes is a flush that buyers are taking. Reply with a noul from 0 to 1.",
    },
    {
        "name": "chase",
        "type": "noul",
        "instruction": "How much of the last hour is a chase. Reply with a noul from 0 to 1.",
    },
)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def score_of(alive: float, organic: float, flush: float, chase: float) -> int:
    return round(100 * (alive + organic + flush + (1 - chase)) / 4)


def gate(alive, organic, flush, chase, *, min_alive=0.70, min_organic=0.65, max_chase=0.30, min_score=70):
    """Code gate. Each miss is a reject. Score uses all four nouls."""
    failed = []
    if float(alive) < float(min_alive):
        failed.append({"rule": "alive_6h", "value": float(alive)})
    if float(organic) < float(min_organic):
        failed.append({"rule": "organic", "value": float(organic)})
    if float(chase) > float(max_chase):
        failed.append({"rule": "chase", "value": float(chase)})
    score = score_of(float(alive), float(organic), float(flush), float(chase))
    if score < float(min_score):
        failed.append({"rule": "score", "value": score})
    return {
        "alive_6h": float(alive),
        "organic": float(organic),
        "flush": float(flush),
        "chase": float(chase),
        "score": score,
        "passed": not failed,
        "failed": failed,
    }


def rules_scores(dex: dict, bundled: int, settings: dict) -> dict:
    volume = float(dex.get("volumeH1") or 0)
    buys_h1 = float(dex.get("txnsH1Buys") or 0)
    buys_m5 = float(dex.get("txnsM5Buys") or 0)
    sells_m5 = float(dex.get("txnsM5Sells") or 0)
    change_m5 = float(dex.get("priceChangeM5") or 0)
    change_h1 = float(dex.get("priceChangeH1") or 0)
    alive = clamp(volume / float(settings["rules.alive_volume_h1"]))
    organic = clamp(buys_h1 / float(settings["rules.organic_buys_h1"]))
    if bundled >= 3:
        organic *= float(settings["rules.bundled_penalty"])
    drop = float(settings["rules.flush_drop_m5_pct"])
    if change_m5 <= -drop and buys_m5 >= sells_m5:
        flush = 1.0
    elif change_m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp(change_h1 / float(settings["rules.chase_h1_pct"]))
    return gate(
        alive,
        organic,
        flush,
        chase,
        min_alive=settings["analyst.min_alive_6h"],
        min_organic=settings["analyst.min_organic"],
        max_chase=settings["analyst.max_chase"],
        min_score=settings["analyst.min_score"],
    )


def failed_text(failed: list) -> str:
    if not failed:
        return ""
    item = failed[0]
    value = item["value"]
    if isinstance(value, float):
        shown = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        shown = str(value)
    return f"{item['rule']} {shown}"


def activity_row(name: str, result: dict, model: str) -> str:
    if model == "rules":
        return f"Analyst (rules) · {failed_text(result['failed'])}"
    return f"Analyst · {failed_text(result['failed'])}"


def build_state(coin: dict, dex: dict, audit: dict) -> str:
    """Numbers only. No opinions, thresholds, balance, or the word buy."""
    text = (
        f"Coin {coin.get('name') or ''} on Solana, graduated {coin.get('ageMin') or 0:.0f} min ago. "
        f"Liquidity ${dex.get('liquidityUsd') or 0:.0f}. Market cap ${dex.get('marketCap') or 0:.0f}.\n"
        f"Last 5 min: {dex.get('txnsM5Buys') or 0:.0f} buys, {dex.get('txnsM5Sells') or 0:.0f} sells, "
        f"volume ${dex.get('volumeM5') or 0:.0f}, price {dex.get('priceChangeM5') or 0:g}%.\n"
        f"Last 1h: {dex.get('txnsH1Buys') or 0:.0f} buys, {dex.get('txnsH1Sells') or 0:.0f} sells, "
        f"volume ${dex.get('volumeH1') or 0:.0f}, price {dex.get('priceChangeH1') or 0:g}%.\n"
        f"Top 10 hold {audit.get('top10') or 0:g}%. Dev holds {audit.get('dev') or 0:g}%, sold 0. "
        f"LP {audit.get('lp') or ''}. Bundled {audit.get('bundled') or ''}. "
        f"Delegations {audit.get('delegations') or ''}."
    )
    return text[:3000]


def _noul(payload, name):
    try:
        value = payload["answers"][name]["noul"]
        number = float(value)
    except (KeyError, TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


async def ask_jev(state: str, api_key: str, timeout_s: int) -> tuple[dict | None, str]:
    body = {"model": "jev-latest", "state": state, "questions": list(QUESTIONS)}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(JEV_URL, json=body, headers=headers)
    if response.status_code == 429:
        return None, "429"
    if response.status_code >= 400:
        return None, "http"
    try:
        payload = response.json()
    except ValueError:
        return None, "bad answer"
    return payload, "ok"


class Analyst:
    def __init__(self, db, settings, bus, api_key: str = "") -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.api_key = api_key
        self.asked: set[str] = set()

    async def run(self) -> None:
        while True:
            message = await self.bus.analyst.get()
            try:
                if not self.settings.get("agent.analyst.paused"):
                    await self.handle(message)
            except Exception as exc:
                log(LOG, f"analyst error {exc.__class__.__name__}")
            self.db.heartbeat("analyst")

    async def handle(self, message: dict) -> None:
        mint = message["mint"]
        if mint in self.asked:
            return
        self.asked.add(mint)
        settings = self.settings.all()
        mode = settings["analyst.mode"]
        if mode == "jev" and not self.api_key:
            mode = "rules"
        dex = message.get("dex") or {}
        bundled = int(message.get("bundled") or 0)
        model = "rules"
        tokens = 0
        if mode == "jev":
            result, model, tokens = await self._jev(message, settings)
            if result is None:
                self._reject(message, {"failed": [{"rule": "no answer", "value": ""}]}, "jev")
                return
        else:
            result = rules_scores(dex, bundled, settings)
            model = "rules"
        self.db.add_verdict(
            {
                "mint": mint,
                "ts": iso(utc_now()),
                "alive_6h": result["alive_6h"],
                "organic": result["organic"],
                "flush": result["flush"],
                "chase": result["chase"],
                "score": result["score"],
                "pass": int(result["passed"]),
                "reason": failed_text(result["failed"]),
                "input_tokens": tokens,
            }
        )
        output = {
            "agent": "analyst",
            "mint": mint,
            "answers": {
                "alive_6h": result["alive_6h"],
                "organic": result["organic"],
                "flush": result["flush"],
                "chase": result["chase"],
            },
            "score": result["score"],
            "passed": result["passed"],
            "failed": result["failed"],
            "model": model,
            "inputTokens": tokens,
        }
        log(LOG, f"{mint} {model} score {result['score']} pass {result['passed']}")
        if not result["passed"]:
            self._reject(message, result, model)
            return
        message = dict(message)
        message.update(output)
        message["analystPrice"] = dex.get("priceUsd")
        await self.bus.executor.put(message)

    async def _jev(self, message, settings):
        state = build_state(
            message,
            message.get("dex") or {},
            {
                "top10": message.get("top10Pct"),
                "dev": message.get("devPct"),
                "lp": message.get("lpText"),
                "bundled": message.get("bundled"),
                "delegations": message.get("delegationsText"),
            },
        )
        if "buy" in state.lower():
            state = state.replace("buy", "").replace("Buy", "")
        timeout_s = int(settings["analyst.timeout_s"])
        last_raw = ""
        for attempt in (1, 2):
            try:
                payload, status = await ask_jev(state, self.api_key, timeout_s)
            except httpx.TimeoutException:
                payload, status = None, "timeout"
            except httpx.HTTPError:
                payload, status = None, "http"
            last_raw = status
            if status == "429":
                await asyncio.sleep(60)
                payload, status = None, "429"
                break
            if payload and status == "ok":
                values = {name: _noul(payload, name) for name in ("alive_6h", "organic", "flush", "chase")}
                if any(v is None for v in values.values()):
                    log(LOG, f"{message['mint']} bad answer")
                    self._reject(message, {"failed": [{"rule": "bad answer", "value": ""}]}, "jev")
                    return None, "jev", 0
                usage = 0
                if isinstance(payload.get("usage"), dict):
                    usage = int(payload["usage"].get("input_tokens") or payload["usage"].get("prompt_tokens") or 0)
                result = gate(
                    values["alive_6h"],
                    values["organic"],
                    values["flush"],
                    values["chase"],
                    min_alive=settings["analyst.min_alive_6h"],
                    min_organic=settings["analyst.min_organic"],
                    max_chase=settings["analyst.max_chase"],
                    min_score=settings["analyst.min_score"],
                )
                return result, str(payload.get("model") or "jev-latest"), usage
            if attempt == 1 and status in ("http", "timeout", "bad answer"):
                await asyncio.sleep(2)
                continue
            break
        log(LOG, f"{message['mint']} jev {last_raw}")
        return None, "jev", 0

    def _reject(self, message, result, model) -> None:
        name = message.get("name") or message["mint"]
        line = activity_row(name, result, model)
        self.db.add_activity(
            "rejected",
            message["mint"],
            "Analyst",
            f"Rejected {name}",
            line,
            "Skipped",
        )
