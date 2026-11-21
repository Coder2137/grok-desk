"""A hung lookup must not freeze the auditor heartbeat, and it must not pass."""

import asyncio

import httpx

from agents.auditor import Auditor, audit_from_facts
from bus import Bus
from config import DeskSettings, defaults
from db import Database
from sources import PUBLIC_RPC

MINT = "HeartbeatMint11111111111111111111111111"
POOL = "H" * 32


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    settings.set("auditor.retries", "1", "test")
    return db, settings, Bus()


def test_a_hanging_lookup_keeps_beating_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.auditor.HEARTBEAT_S", 0.02)
    monkeypatch.setattr("agents.auditor.AUDIT_LIMIT_S", 0.05)
    db, settings, bus = _desk(tmp_path)
    try:
        auditor = Auditor(db, settings, bus)
        started = asyncio.Event()

        async def hang(_message):
            started.set()
            await asyncio.Event().wait()

        auditor.gather = hang
        bus.auditor.put_nowait({"mint": MINT, "name": "Hang"})

        async def run():
            task = asyncio.create_task(auditor.run())
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        beats = db.conn.execute(
            "SELECT COUNT(*) AS n FROM heartbeats WHERE agent = 'auditor'"
        ).fetchone()["n"]
        assert beats >= 2
        rows = db.audits_for(MINT)
        assert rows
        assert rows[0]["value_text"] == "retrying"
        assert rows[0]["pass"] is None
        assert all(row["pass"] != 1 for row in rows)
        assert bus.analyst.empty()
        assert "timeout" not in "".join(row["value_text"] or "" for row in rows) or rows[0]["value_text"] == "retrying"
    finally:
        db.close()


def test_an_endless_signature_walk_stops_and_does_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.auditor._MAX_SIGNATURE_PAGES", 2)
    db, settings, _bus = _desk(tmp_path)
    auditor = Auditor(db, settings, Bus())

    async def no_sleep(_seconds):
        return None

    auditor._sleep = no_sleep
    calls = {"n": 0}

    class Endless:
        async def post(self, url, json=None, timeout=None):
            calls["n"] += 1
            rows = [
                {"signature": f"page-{calls['n']}-{index}", "slot": index, "err": None}
                for index in range(1000)
            ]
            return httpx.Response(200, json={"result": rows})

    async def walk():
        return await auditor._signature_pages(Endless(), PUBLIC_RPC, POOL, 1)

    try:
        rows, error, truncated = asyncio.run(walk())
        assert rows is None
        assert error == "timeout"
        assert truncated is True
        assert calls["n"] == 2
        failed = audit_from_facts(
            {
                "mint": MINT,
                "ts": "2026-11-21T07:07:00+00:00",
                "settings": defaults(),
                "timed_out": True,
            }
        )
        assert failed["passed"] is False
        assert failed["reason"] == "no data: timeout"
        assert failed["layers"][0]["pass"] is False
    finally:
        db.close()
