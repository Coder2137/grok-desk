import asyncio
import json

import pytest

from agents.scanner import Scanner, judge
from api import Writer
from bus import Bus
from config import DeskSettings, defaults
from db import Database
from instance import DESK2_LIQUIDITY, apply_instance, resolve_instance, setting_overrides
from livecrypto import decrypt_envelope, envelope_bytes, encrypt_envelope, is_live_path
from publish_live import load_live_files
from telegram import Telegram

FIXTURE_DATA_PATH = "fixture-path-0123456789abcdef0123456789"
MINT = "Desk2FanMint11111111111111111111111111"


def _metrics(liquidity):
    return {
        "known": True,
        "liquidity": liquidity,
        "top10": 10,
        "h1": 1,
        "age_min": 20,
        "name": "Coin",
        "symbol": "COIN",
    }


def test_desk1_liquidity_default_stays_40k():
    assert defaults()["scanner.min_liquidity_usd"] == 40000
    assert defaults()["desk.paper_start_stack"] == 200
    assert defaults()["desk.stack"] == 200


def test_instances_keep_separate_db_balance_and_liquidity(tmp_path):
    desk1_env = {"DESK_DB": "var/desk.db", "JSON_ROOT": "var/json"}
    apply_instance("desk1", desk1_env)
    assert desk1_env["DESK_DB"] == "var/desk.db"
    assert "DESK_LIVE_PREFIX" not in desk1_env
    assert "DESK_TELEGRAM" not in desk1_env

    desk2_env = {"DESK_DB": "var/desk.db", "JSON_ROOT": "var/json"}
    apply_instance("desk2", desk2_env)
    assert desk2_env["DESK_DB"] == "var/desk2.db"
    assert desk2_env["JSON_ROOT"] == "var/desk2/json"
    assert desk2_env["DESK_LIVE_PREFIX"] == "desk2"
    assert desk2_env["DESK_TELEGRAM"] == "0"
    custom = {"DESK_DB": "var/custom.db"}
    apply_instance("desk2", custom)
    assert custom["DESK_DB"] == "var/custom.db"

    assert resolve_instance(["main.py", "--instance", "desk2"], {}) == "desk2"
    assert resolve_instance(["main.py"], {"DESK_INSTANCE": "desk1"}) == "desk1"
    with pytest.raises(SystemExit):
        resolve_instance(["main.py"], {"DESK_INSTANCE": "live"})

    one = Database(tmp_path / "desk.db")
    two = Database(tmp_path / "desk2.db")
    try:
        settings1 = DeskSettings(one)
        settings2 = DeskSettings(two, overrides=setting_overrides("desk2"))
        assert settings1.get("scanner.min_liquidity_usd") == 40000
        assert settings2.get("scanner.min_liquidity_usd") == DESK2_LIQUIDITY
        assert settings1.get("desk.paper_start_stack") == 200
        assert settings2.get("desk.stack") == 200
        assert settings2.set("desk.stack", "250", "test")[0] is True
        assert settings1.get("desk.stack") == 200
        assert float(settings2.get("desk.stack")) == 250
        two.heartbeat("main")
        assert "main" not in one.latest_heartbeats()
        assert "main" in two.latest_heartbeats()
        low = _metrics(15000)
        assert judge(low, settings1.all(), [])[0] == "kill"
        assert judge(low, settings1.all(), [])[1].startswith("liq")
        assert judge(low, settings2.all(), []) == ("pass", None)
        poorer = _metrics(5000)
        assert judge(poorer, settings1.all(), [])[0] == "kill"
        assert judge(poorer, settings2.all(), [])[0] == "kill"
    finally:
        one.close()
        two.close()


def test_live_mode_uses_the_same_paper_guard(tmp_path):
    one = Database(tmp_path / "desk.db")
    two = Database(tmp_path / "desk2.db")
    try:
        settings1 = DeskSettings(one)
        settings2 = DeskSettings(two, overrides=setting_overrides("desk2"))
        ok1, message1 = settings1.set("desk.mode", "LIVE", "test")
        ok2, message2 = settings2.set("desk.mode", "LIVE", "test")
        assert ok1 is False
        assert ok2 is False
        assert message1 == message2 == "need 1 full PAPER night"
        assert settings1.get("desk.mode") == "PAPER"
        assert settings2.get("desk.mode") == "PAPER"
    finally:
        one.close()
        two.close()


def test_one_websocket_feeds_desk2_from_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_FEED", str(tmp_path / "migrations.jsonl"))
    monkeypatch.setenv("DESK_FEED_OFFSET", str(tmp_path / "feed.offset"))
    connects = []

    class Boom:
        def __init__(self, *args, **kwargs):
            connects.append(1)

        async def __aenter__(self):
            raise RuntimeError("no second socket")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("agents.scanner.websockets.connect", lambda *args, **kwargs: Boom())
    one = Database(tmp_path / "desk.db")
    two = Database(tmp_path / "desk2.db")
    try:
        source = Scanner(one, DeskSettings(one), Bus(), source=True)
        follower = Scanner(two, DeskSettings(two, overrides=setting_overrides("desk2")), Bus(), source=False)
        frame = {"txType": "migrate", "mint": MINT, "pool": "pump-amm", "signature": "sig"}
        source.ingest_message(json.dumps(frame), relay=True)
        assert MINT in source.waiting
        follower.follow_once(0)
        assert MINT in follower.waiting
        assert connects == []
        again = (tmp_path / "migrations.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(again) == 1

        async def both():
            socket = asyncio.create_task(source._socket())
            follow = asyncio.create_task(follower._follow())
            await asyncio.sleep(0.05)
            socket.cancel()
            follow.cancel()
            await asyncio.gather(socket, follow, return_exceptions=True)

        asyncio.run(both())
        assert connects == [1]
    finally:
        one.close()
        two.close()


def test_feed_append_failure_does_not_stop_desk1(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_FEED", str(tmp_path))
    db = Database(tmp_path / "desk.db")
    try:
        scanner = Scanner(db, DeskSettings(db), Bus(), source=True)
        frame = {"txType": "migrate", "mint": MINT, "pool": "pump-amm", "signature": "sig"}
        scanner.ingest_message(json.dumps(frame), relay=True)
        assert MINT in scanner.waiting
    finally:
        db.close()


def test_desk2_telegram_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_TELEGRAM", "0")
    monkeypatch.setenv("TG_TOKEN", "fixture-telegram-token")

    def refuse(*_args, **_kwargs):
        raise AssertionError("telegram client opened")

    monkeypatch.setattr("telegram.httpx.AsyncClient", refuse)
    db = Database(tmp_path / "desk2.db")
    try:
        bus = Bus()
        telegram = Telegram(db, DeskSettings(db, overrides=setting_overrides("desk2")), bus, None, None)
        assert telegram.enabled() is False

        async def run():
            task = asyncio.create_task(telegram._events())
            ack = asyncio.get_running_loop().create_future()
            await bus.events.put({"type": "buy", "mint": MINT, "text": "PAPER · Buy", "link": "https://fomo.family/", "ack": ack})
            assert await asyncio.wait_for(ack, timeout=1) is False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
    finally:
        db.close()


def test_desk2_live_files_publish_beside_desk1(tmp_path, monkeypatch):
    assert is_live_path("state.json")
    assert is_live_path("desk2/state.json")
    assert is_live_path("desk2/coin/Mint111.json")
    assert not is_live_path("desk2/.plain-hashes.json")
    assert not is_live_path("desk2/coin/../state.json")
    root = tmp_path / "live"
    (root / "coin").mkdir(parents=True)
    (root / "desk2" / "coin").mkdir(parents=True)
    samples = {
        "state.json": b'{"balance":1}',
        "chart.json": b'{"points":[]}',
        "activity.json": b'{"rows":[]}',
        "coin/Mint111.json": b'{"ok":true}',
        "desk2/state.json": b'{"balance":2}',
        "desk2/chart.json": b'{"points":[]}',
        "desk2/activity.json": b'{"rows":[]}',
        "desk2/coin/Mint222.json": b'{"ok":true}',
    }
    for name, raw in samples.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(envelope_bytes(encrypt_envelope(raw, FIXTURE_DATA_PATH, name, nonce=bytes(range(12)))))
    (root / "desk2" / ".plain-hashes.json").write_text("{}", encoding="utf-8")
    found = load_live_files(root)
    assert set(found) == set(samples)
    assert "desk2/.plain-hashes.json" not in found

    monkeypatch.setenv("DATA_PATH", FIXTURE_DATA_PATH)
    monkeypatch.setenv("JSON_ROOT", str(tmp_path / "json"))
    monkeypatch.setenv("LIVE_ROOT", str(root))
    monkeypatch.setenv("DESK_LIVE_PREFIX", "desk2")
    db = Database(tmp_path / "desk2.db")
    try:
        Writer(db, DeskSettings(db, overrides=setting_overrides("desk2")), Bus()).write_all()
        live_state = root / "desk2" / "state.json"
        assert live_state.is_file()
        plain = tmp_path / "json" / "d" / FIXTURE_DATA_PATH / "desk2" / "state.json"
        assert plain.is_file()
        envelope = json.loads(live_state.read_text(encoding="utf-8"))
        assert decrypt_envelope(envelope, FIXTURE_DATA_PATH, "desk2/state.json") == plain.read_bytes()
        assert (root / "desk2" / ".plain-hashes.json").is_file()
        assert "desk2/state.json" in json.loads((root / "desk2" / ".plain-hashes.json").read_text(encoding="utf-8"))
    finally:
        db.close()
