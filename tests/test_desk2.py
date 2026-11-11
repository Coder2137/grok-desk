import asyncio
import json
from pathlib import Path

import pytest

from agents.scanner import Scanner, judge
from api import Writer
from bus import Bus
from config import DeskSettings, defaults
from db import Database
from instance import (
    DESK2_LIQUIDITY,
    DESK3_LIQUIDITY,
    apply_instance,
    display_name,
    holds_socket,
    resolve_instance,
    setting_overrides,
)
from livecrypto import decrypt_envelope, envelope_bytes, encrypt_envelope, is_live_path
from logutil import file_logger, log
from publish_live import load_live_files
from secrets_env import force_live_refused, load_user_secrets, parse_env_text
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


def test_desk3_is_its_own_20k_paper_desk(tmp_path):
    env = {"DESK_DB": "var/desk.db", "JSON_ROOT": "var/json", "DESK_TELEGRAM": "1"}
    apply_instance("desk3", env)
    assert env["DESK_DB"] == "var/desk3.db"
    assert env["JSON_ROOT"] == "var/desk3/json"
    assert env["DESK_LIVE_PREFIX"] == "desk3"
    assert env["LOG_DIR"] == "logs/desk3"
    assert env["DESK_FEED_OFFSET"] == "var/desk3/feed.offset"
    assert env["DESK_TELEGRAM"] == "0"
    assert env["LOCAL_PORT"] == "8767"
    assert display_name("desk1") == "Desk 40k"
    assert display_name("desk2") == "Desk 10k"
    assert display_name("desk3") == "Desk 20k"
    assert resolve_instance(["main.py", "--instance", "desk3"], {}) == "desk3"
    assert holds_socket("desk1") is True
    assert holds_socket("desk2") is False
    assert holds_socket("desk3") is False
    db = Database(tmp_path / "desk3.db")
    try:
        settings = DeskSettings(db, overrides=setting_overrides("desk3"))
        assert settings.get("scanner.min_liquidity_usd") == DESK3_LIQUIDITY
        assert settings.get("desk.stack") == 200
        assert settings.get("desk.mode") == "PAPER"
        assert settings.set("desk.mode", "LIVE", "test") == (False, "need 1 full PAPER night")
        assert judge(_metrics(15000), settings.all(), [])[0] == "kill"
        assert judge(_metrics(25000), settings.all(), []) == ("pass", None)
        assert judge(_metrics(25000), defaults(), [])[0] == "kill"
    finally:
        db.close()
    assert is_live_path("desk3/state.json")
    assert is_live_path("desk3/coin/Mint333.json")
    assert not is_live_path("desk3/.plain-hashes.json")
    root = tmp_path / "live"
    (root / "desk3").mkdir(parents=True)
    name = "desk3/state.json"
    (root / name).write_bytes(
        envelope_bytes(encrypt_envelope(b'{"balance":3}', FIXTURE_DATA_PATH, name, nonce=bytes(range(12))))
    )
    for core in ("state.json", "chart.json", "activity.json"):
        (root / core).write_bytes(
            envelope_bytes(encrypt_envelope(b"{}", FIXTURE_DATA_PATH, core, nonce=bytes(range(12))))
        )
    found = load_live_files(root)
    assert "desk3/state.json" in found
    assert "state.json" in found
    script = Path("run-desk3.ps1").read_text(encoding="utf-8")
    assert '$env:DESK_INSTANCE = "desk3"' in script
    assert '$env:DESK_TELEGRAM = "0"' in script
    assert "FORCE_LIVE is refused." in script
    assert ".desk-secrets" in script
    cmd = Path("GrokDesk3.cmd").read_text(encoding="utf-8")
    assert r"%USERPROFILE%\grok-desk\run-desk3.ps1" in cmd
    assert "DESK_HOME" in cmd
    assert "kwiat" not in cmd
    page = Path("web/app.js").read_text(encoding="utf-8")
    assert page.index("Desk 10k") < page.index("Desk 20k") < page.index("Desk 40k")


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
    assert desk2_env["DESK_INSTANCE"] == "desk2"
    forced = {"DESK_TELEGRAM": "1", "DESK_DB": "var/other.db"}
    apply_instance("desk2", forced)
    assert forced["DESK_TELEGRAM"] == "0"
    assert forced["DESK_DB"] == "var/other.db"
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


def test_secrets_file_parsing_never_keeps_comments_and_refuses_force_live(tmp_path):
    text = """
# comment
DATA_PATH="fixture-path-0123456789abcdef0123456789"
export DESK_TELEGRAM=1
set FORCE_LIVE=yes
$env:FOMO_HANDLE = 'operator'
blank
"""
    parsed = parse_env_text(text)
    assert parsed["DATA_PATH"] == "fixture-path-0123456789abcdef0123456789"
    assert parsed["DESK_TELEGRAM"] == "1"
    assert parsed["FORCE_LIVE"] == "yes"
    assert parsed["FOMO_HANDLE"] == "operator"
    assert "blank" not in parsed
    env = {"DATA_PATH": "already-set-path-0123456789abcdef"}
    secret = tmp_path / "env"
    secret.write_text(text, encoding="utf-8")
    load_user_secrets(env, secret)
    assert env["DATA_PATH"] == "already-set-path-0123456789abcdef"
    assert env["FOMO_HANDLE"] == "operator"
    assert force_live_refused(env) is True
    assert force_live_refused({"FORCE_LIVE": "0"}) is False
    assert force_live_refused({}) is False
    missing = tmp_path / "missing-env"
    try:
        load_user_secrets({}, missing)
    except OSError as exc:
        assert "fixture-path" not in str(exc)


def test_desk2_logs_follow_log_dir_after_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOG_DIR", raising=False)
    logger = file_logger("desk2-log-test", "scanner.log")
    assert not (tmp_path / "logs" / "scanner.log").exists()
    monkeypatch.setenv("LOG_DIR", "logs/desk2")
    log(logger, "desk2 line")
    written = tmp_path / "logs" / "desk2" / "scanner.log"
    assert written.is_file()
    assert written.read_text(encoding="utf-8").strip() == "desk2 line"
    assert not (tmp_path / "logs" / "scanner.log").exists()
    monkeypatch.delenv("LOG_DIR", raising=False)
    other = file_logger("desk1-log-test", "main.log")
    log(other, "desk1 line")
    assert (tmp_path / "logs" / "main.log").read_text(encoding="utf-8").strip() == "desk1 line"


def test_launcher_files_keep_telegram_off_and_do_not_hardcode_a_user():
    script = Path("run-desk2.ps1").read_text(encoding="utf-8")
    assert ".desk-secrets" in script
    assert "FORCE_LIVE is refused." in script
    assert '$env:DESK_INSTANCE = "desk2"' in script
    assert '$env:DESK_TELEGRAM = "0"' in script
    assert "Write-Host" not in script
    cmd = Path("GrokDesk2.cmd").read_text(encoding="utf-8")
    assert r"%USERPROFILE%\grok-desk\run-desk2.ps1" in cmd
    assert "DESK_HOME" in cmd
    assert "kwiat" not in cmd
    assert "WindowStyle Hidden" in cmd
