"""One asyncio process: agents, Telegram long polling, and the plan B JSON writer."""

import asyncio
import os
import sys
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.executor import Executor
from agents.risk import Risk
from agents.scanner import Scanner
from api import Writer, json_root
from bus import Bus
from config import DeskSettings, utc_now
from db import Database
from instance import apply_instance, holds_socket, loads_secrets, resolve_instance, setting_overrides
from secrets_env import force_live_refused, load_user_secrets
from fields import dex_fields, pumpswap_pair
from logutil import file_logger, log
from sources import dexscreener_batch
from telegram import Telegram

LOG = file_logger("main", "main.log")


def load_env() -> None:
    path = Path(".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def read_dex(mints: list[str]) -> list[dict]:
    async with httpx.AsyncClient() as client:
        payload = await dexscreener_batch(mints, client)
    grouped: dict[str, list] = {}
    for row in payload:
        if isinstance(row, dict):
            mint = ((row.get("baseToken") or {}).get("address"))
            if mint:
                grouped.setdefault(mint, []).append(row)
    out = []
    for mint, rows in grouped.items():
        pair = pumpswap_pair(rows)
        if not pair:
            continue
        fields = dex_fields(pair)
        fields["mint"] = mint
        out.append(fields)
    return out


def local_app() -> FastAPI:
    local = FastAPI()
    web = Path("web")

    @local.get("/d")
    @local.get("/d/")
    def bare_d():
        raise HTTPException(status_code=404)

    @local.get("/d/{secret}/{file_path:path}")
    def secret_file(secret: str, file_path: str):
        root = json_root()
        if root is None or secret != root.name or ".." in file_path:
            raise HTTPException(status_code=404)
        path = (root / file_path).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path)

    if web.is_dir():
        local.mount("/", StaticFiles(directory=web, html=True), name="web")
    return local


async def serve_local() -> None:
    host = os.environ.get("LOCAL_HOST", "127.0.0.1")
    port = int(os.environ.get("LOCAL_PORT", "8765"))
    config = uvicorn.Config(local_app(), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def heart(db, settings, bus) -> None:
    note = settings.ticket_note()
    if note:
        await bus.events.put({"type": "text", "text": note})
    while True:
        db.heartbeat("main")
        rolled = settings.roll_night(utc_now(), announce=True)
        if rolled:
            await bus.events.put({"type": "text", "text": rolled})
        await asyncio.sleep(30)


async def run() -> None:
    load_env()
    name = resolve_instance()
    if loads_secrets(name):
        load_user_secrets()
    apply_instance(name)
    if loads_secrets(name) and force_live_refused(os.environ):
        print("FORCE_LIVE is refused.", file=sys.stderr)
        raise SystemExit(1)
    db = Database(os.environ.get("DESK_DB", "var/desk.db"))
    settings = DeskSettings(db, overrides=setting_overrides(name))
    if not (os.environ.get("TYPESAFE_API_KEY") or "").strip() and settings.get("analyst.mode") != "rules":
        settings.set("analyst.mode", "rules", "boot")
    bus = Bus()
    scanner = Scanner(db, settings, bus, source=holds_socket(name))
    auditor = Auditor(db, settings, bus)
    analyst = Analyst(db, settings, bus, api_key=os.environ.get("TYPESAFE_API_KEY") or "")
    executor = Executor(db, settings, bus)

    async def dex_reader(mints: list[str]):
        rows = await read_dex(mints)
        for row in rows:
            bus.market[row["mint"]] = row
        return rows

    risk = Risk(db, settings, bus, dex_reader)
    telegram = Telegram(db, settings, bus, executor, risk)
    writer = Writer(db, settings, bus)
    tasks = [
        scanner.run(),
        auditor.run(),
        analyst.run(),
        executor.run(),
        risk.run(),
        telegram.run(),
        writer.run(),
        heart(db, settings, bus),
    ]
    if os.environ.get("LOCAL_API") == "1":
        tasks.append(serve_local())
    log(LOG, "desk up" if name == "desk1" else f"{name} up")
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
