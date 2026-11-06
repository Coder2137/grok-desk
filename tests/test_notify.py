import asyncio
from pathlib import Path

import pytest

from agents.executor import Executor
from agents.risk import Risk
from bus import Bus
from config import DeskSettings, iso, utc_now
from db import Database
from links import coin_url
from telegram import Telegram
from webpush import notify

MINT = "ReplayMint1111111111111111111111111"
TOKEN = "fixture-telegram-token"
OWNER = "42"
BANNED = ("utm_", "ref=", "referral", "affiliate", "onelink", "bit.ly", "t.co", "tinyurl", "?")


class Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class Client:
    def __init__(self, status_code=200, explode=None):
        self.status_code = status_code
        self.explode = explode
        self.posts = []
        self.gets = []

    async def post(self, url, json=None):
        self.posts.append((url, json))
        if self.explode:
            raise self.explode
        return Response(self.status_code)

    async def get(self, url, params=None):
        self.gets.append(params)
        return Response(200, {"result": []})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _desk(tmp_path):
    db = Database(tmp_path / "desk.db")
    settings = DeskSettings(db)
    return db, settings


def _owns(db, settings):
    db.set_setting("desk.owner_chat_id", OWNER, "test")
    return Telegram(db, settings, Bus(), None, None)


def _position(db, status="open"):
    db.add_position(
        {
            "mint": MINT,
            "opened_at": iso(utc_now()),
            "entry_price": 1,
            "size_usd": 47.50,
            "peak_price": 1,
            "stop_price": 0.94,
            "trail_active": 0,
            "closed_at": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_usd": None,
            "fees_usd": None,
            "mode": "PAPER",
            "stop_pct": 6,
            "trail_start_pct": 20,
            "trail_pct": 12,
            "timeout_h": 3,
            "timeout_min_gain_pct": 5,
            "status": status,
            "name": "Replay",
        }
    )


def _assert_fomo(url):
    assert url == coin_url(MINT, "solana")
    assert url.startswith("https://fomo.family/tokens/solana/")
    lowered = url.lower()
    for banned in BANNED:
        assert banned not in lowered


def test_web_push_is_not_the_notifier():
    assert notify("hi", token="unused") is False


def test_missing_token_or_owner_does_not_post(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        tg = Telegram(db, settings, Bus(), None, None)
        tg.token = ""
        client = Client()

        async def run():
            assert await tg._deliver(client, {"type": "text", "text": "hi"}) is False
            tg.token = TOKEN
            assert await tg._deliver(client, {"type": "text", "text": "hi"}) is False

        asyncio.run(run())
        assert client.posts == []
    finally:
        db.close()


def test_only_the_owner_is_targeted_and_links_stay_clean(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        claimed = Telegram(db, settings, Bus(), None, None).handle_text("7", "/start")
        assert claimed and "PAPER" in claimed[0]["text"]
        assert settings.get("desk.owner_chat_id") == "7"
        stranger = Telegram(db, settings, Bus(), None, None)
        assert stranger.handle_text("8", "/start") == []
        assert stranger.handle_text("8", "LIVE") == []
        assert settings.get("desk.owner_chat_id") == "7"
        assert settings.get("desk.mode") == "PAPER"

        db.set_setting("desk.owner_chat_id", OWNER, "test")
        db.upsert_coin(mint=MINT, name="Replay", symbol="RPL")
        tg = Telegram(db, settings, Bus(), None, None)
        tg.token = TOKEN
        replies = tg.handle_text(OWNER, f"buy {MINT}")
        assert len(replies) == 1
        _assert_fomo(replies[0]["keyboard"][0][0]["url"])
        assert replies[0]["keyboard"][0][0]["text"] == "Buy on FOMO"
        assert "not sized, not on the desk" in replies[0]["text"]

        client = Client()

        async def deliver():
            link = coin_url(MINT, "solana")
            assert await tg._deliver(client, {"type": "buy", "mint": MINT, "text": "PAPER · Buy Replay", "link": link})
            assert await tg._deliver(
                client,
                {"type": "sell", "mint": MINT, "text": "PAPER · Sold Replay", "link": link, "sold": True},
            )
            assert await tg._deliver(
                client,
                {"type": "sell", "mint": MINT, "text": "Replay: no price for 1 min", "link": link, "sold": False},
            )

        asyncio.run(deliver())
        assert len(client.posts) == 3
        for url, payload in client.posts:
            assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            assert payload["chat_id"] == OWNER
            assert payload["disable_web_page_preview"] is True
            rows = payload["reply_markup"]["inline_keyboard"]
            _assert_fomo(rows[0][0]["url"])
        buy_rows = client.posts[0][1]["reply_markup"]["inline_keyboard"]
        assert buy_rows[1][0]["callback_data"] == f"bought:{MINT}"
        assert buy_rows[1][1]["callback_data"] == f"skip:{MINT}"
        assert "Sold" in client.posts[1][1]["reply_markup"]["inline_keyboard"][1][0]["text"]
        assert len(client.posts[2][1]["reply_markup"]["inline_keyboard"]) == 1
    finally:
        db.close()


def test_failed_send_returns_false_and_does_not_log_the_token(tmp_path, monkeypatch):
    db, settings = _desk(tmp_path)
    try:
        db.set_setting("desk.owner_chat_id", OWNER, "test")
        bus = Bus()
        tg = Telegram(db, settings, bus, None, None)
        tg.token = TOKEN
        client = Client(status_code=500)

        async def status():
            assert await tg._deliver(client, {"type": "text", "text": "hi"}) is False

        asyncio.run(status())
        assert client.posts

        marker = "notify-test-secret-marker"
        loop_client = Client(explode=RuntimeError(marker))

        class Factory:
            def __call__(self, *args, **kwargs):
                return loop_client

        monkeypatch.setattr("telegram.httpx.AsyncClient", Factory())
        event = {"type": "sell", "mint": MINT, "text": "sell", "link": coin_url(MINT, "solana"), "ack": None}

        async def once():
            event["ack"] = asyncio.get_running_loop().create_future()

            async def get():
                if not get.done:
                    get.done = True
                    return event
                raise asyncio.CancelledError

            get.done = False
            bus.events.get = get
            with pytest.raises(asyncio.CancelledError):
                await tg._events()
            assert event["ack"].result() is False

        asyncio.run(once())
        log_path = Path("logs") / "telegram.log"
        text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        assert marker not in text
        assert TOKEN not in text
    finally:
        db.close()


def test_non_owner_callback_is_ignored(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        db.set_setting("desk.owner_chat_id", OWNER, "test")

        class Commands:
            def __init__(self):
                self.calls = []

            def on_command(self, action, mint):
                self.calls.append((action, mint))

        commands = Commands()
        tg = Telegram(db, settings, Bus(), commands, None)
        tg.token = TOKEN
        client = Client()

        async def run():
            await tg._update(
                client,
                {
                    "callback_query": {
                        "id": "c1",
                        "data": f"bought:{MINT}",
                        "message": {"chat": {"id": 99}},
                    }
                },
            )

        asyncio.run(run())
        assert commands.calls == []
        assert client.posts == []
    finally:
        db.close()


def test_poll_offset_advances_once(tmp_path, monkeypatch):
    db, settings = _desk(tmp_path)
    try:
        tg = Telegram(db, settings, Bus(), None, None)
        tg.token = TOKEN
        client = Client()

        async def get(url, params=None):
            client.gets.append(params["offset"])
            if len(client.gets) == 1:
                return Response(
                    200,
                    {"result": [{"update_id": 4, "message": {"chat": {"id": 9}, "text": "LIVE"}}]},
                )
            raise asyncio.CancelledError

        client.get = get

        class Factory:
            def __call__(self, *args, **kwargs):
                return client

        monkeypatch.setattr("telegram.httpx.AsyncClient", Factory())

        async def run():
            with pytest.raises(asyncio.CancelledError):
                await tg._poll()

        asyncio.run(run())
        assert client.gets == [0, 5]
        assert tg.offset == 5
        assert client.posts == []
        assert settings.get("desk.mode") == "PAPER"
    finally:
        db.close()


def test_buy_push_retries_then_stops_and_a_sent_signal_is_not_repeated(tmp_path, monkeypatch):
    db, settings = _desk(tmp_path)
    try:
        async def no_sleep(_seconds):
            return None

        monkeypatch.setattr("agents.executor.asyncio.sleep", no_sleep)
        bus = Bus()
        executor = Executor(db, settings, bus)
        events = []
        mode = {"fail": True}

        async def consume():
            while True:
                event = await bus.events.get()
                events.append(event)
                event["ack"].set_result(not mode["fail"] and len(events) == 5)

        async def retries():
            task = asyncio.create_task(consume())
            try:
                assert await executor._push(MINT, "PAPER · Buy", coin_url(MINT, "solana")) is False
                assert len(events) == 3
                _assert_fomo(events[0]["link"])
                mode["fail"] = False
                assert await executor._push(MINT, "again", coin_url(MINT, "solana")) is True
                assert len(events) == 5
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(retries())

        message = {
            "mint": MINT,
            "name": "Replay",
            "liquidityUsd": 80_000,
            "dex": {"priceUsd": 0.001},
            "analystPrice": 0.001,
            "pushPrice": 0.001,
            "score": 80,
        }
        pushes = []

        async def record(mint, text, link):
            pushes.append((mint, text, link))
            return False

        executor._push = record

        async def paper():
            await executor.handle(message)
            await executor.handle(message)

        asyncio.run(paper())
        assert len(pushes) == 1
        _assert_fomo(pushes[0][2])
        assert "PAPER · " in pushes[0][1]
        assert db.open_position_for(MINT)["mode"] == "PAPER"
        assert db.signal_tonight(MINT, "1970")["status"] == "bought"
    finally:
        db.close()


def test_skipped_signal_does_not_block_a_later_push(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        settings.set("executor.below_min", "skip", "test")
        bus = Bus()
        executor = Executor(db, settings, bus)
        pushes = []

        async def record(mint, text, link):
            pushes.append(link)
            return True

        executor._push = record
        message = {
            "mint": MINT,
            "name": "Replay",
            "liquidityUsd": 80_000,
            "dex": {"priceUsd": 0.001},
            "analystPrice": 0.001,
            "pushPrice": 0.001,
            "score": 70,
        }

        async def run():
            await executor.handle(message)
            settings.set("executor.below_min", "bump", "test")
            await executor.handle(message)

        asyncio.run(run())
        assert len(pushes) == 1
        _assert_fomo(pushes[0])
    finally:
        db.close()


def test_missing_price_notifies_once_per_four_polls_and_live_nudge_once(tmp_path):
    db, settings = _desk(tmp_path)
    try:
        _position(db)
        bus = Bus()

        async def no_price(_mints):
            return []

        risk = Risk(db, settings, bus, no_price)

        async def polls():
            for _ in range(8):
                await risk.poll()

        asyncio.run(polls())
        notes = []
        while True:
            try:
                notes.append(bus.events.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert len(notes) == 2
        assert [note["sold"] for note in notes] == [False, False]
        assert notes[0]["text"].endswith("no price for 1 min")
        _assert_fomo(notes[0]["link"])

        settings.set("risk.confirm_window_min", 1, "test")
        settings.set("risk.poll_s", 60, "test")
        db.update_position(db.open_position_for(MINT)["id"], status="exiting")
        risk.missed_polls.clear()

        async def nudge():
            row = dict(db.open_position_for(MINT))
            await risk._nudge_live(row)
            await risk._nudge_live(row)

        asyncio.run(nudge())
        nudges = []
        while True:
            try:
                nudges.append(bus.events.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert len(nudges) == 1
        assert nudges[0]["text"] == "Replay still open"
        _assert_fomo(nudges[0]["link"])
    finally:
        db.close()
