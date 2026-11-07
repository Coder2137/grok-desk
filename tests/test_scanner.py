import json

from agents.scanner import SUBSCRIBE_MIGRATION, SUBSCRIPTION_ACK, Scanner, subscription_ack
from bus import Bus
from config import DeskSettings
from db import Database
from fields import migration_frame, rug_lp
import fields


ACK = {"message": SUBSCRIPTION_ACK}
# Public fields from the 2026-11-07 subscribeMigration frame. creator was absent.
# pool is the venue label, not an account address.
OBSERVED = {
    "mint": "CKz3xbwSdsaTQfRQkAG97GQzGLUYd1Rghq7pMasSpump",
    "pool": "pump-amm",
    "signature": "2hHygL79hzwoBWRzmgybXeDUbUjZD6rxPAEyWUZS3TKbNkd6FtxWmLd8xzYfHs1BpTWxWciQ936gCRsTPPPyMkYU",
    "txType": "migrate",
}


def _scanner(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.scanner.RAW_DIR", tmp_path)
    monkeypatch.setattr(fields, "_MIGRATION_FILE", tmp_path / "migration_keys.json")
    lines = []
    monkeypatch.setattr("agents.scanner.log", lambda _logger, message: lines.append(message))
    db = Database(tmp_path / "desk.db")
    scanner = Scanner(db, DeskSettings(db), Bus())
    return scanner, db, lines


def test_subscribe_payload_is_only_migration():
    assert SUBSCRIBE_MIGRATION == {"method": "subscribeMigration"}
    assert "subscribeNewToken" not in json.dumps(SUBSCRIBE_MIGRATION)
    assert "subscribeTokenTrade" not in json.dumps(SUBSCRIBE_MIGRATION)
    assert "subscribeAccountTrade" not in json.dumps(SUBSCRIBE_MIGRATION)


def test_ack_is_not_saved_and_does_not_start_an_audit(tmp_path, monkeypatch):
    scanner, db, lines = _scanner(tmp_path, monkeypatch)
    try:
        scanner.ingest_message(json.dumps(ACK))
        assert not (tmp_path / "migration-sample.json").exists()
        assert scanner.waiting == {}
        assert list(db.coins()) == []
        assert any("migration subscription acknowledged" in line for line in lines)
        assert not any("mint unset" in line or "mint missing" in line for line in lines)
        assert subscription_ack(ACK) is True
        assert subscription_ack({"message": "Subscribed to 'migration' events.", "mint": "fixture"}) is True
    finally:
        db.close()


def test_observed_migration_replaces_the_ack_and_keeps_creator_null(tmp_path, monkeypatch):
    scanner, db, lines = _scanner(tmp_path, monkeypatch)
    try:
        sample = tmp_path / "migration-sample.json"
        sample.write_text(json.dumps(ACK), encoding="utf-8")
        assert migration_frame(ACK) is None
        assert migration_frame(OBSERVED) == {
            "mint": OBSERVED["mint"],
            "pool": "pump-amm",
            "signature": OBSERVED["signature"],
            "creator": None,
        }
        scanner.ingest_message(json.dumps(ACK))
        scanner.ingest_message(json.dumps(OBSERVED))
        assert json.loads(sample.read_text(encoding="utf-8")) == OBSERVED
        assert list(scanner.waiting) == [OBSERVED["mint"]]
        admitted = scanner.waiting[OBSERVED["mint"]]
        assert admitted["pool"] is None
        assert admitted["pool_label"] == "pump-amm"
        assert admitted["signature"] == OBSERVED["signature"]
        assert admitted["creator"] is None
        row = db.coin(OBSERVED["mint"])
        assert row["pool"] is None
        assert row["creator"] is None
        assert scanner.bus.auditor.empty()
        assert not any("mint missing" in line or "mint unset" in line for line in lines)
        markets = [{"pubkey": "FixturePoolAddress111111111111111111111", "lp": {"lpLockedPct": 100}}]
        assert rug_lp({"markets": markets}, "pump-amm") == {"found": False, "locked": None, "burned": None}
        assert rug_lp({"markets": markets}, None) == {"found": False, "locked": None, "burned": None}
    finally:
        db.close()


def test_existing_frame_sample_is_kept(tmp_path, monkeypatch):
    scanner, db, _lines = _scanner(tmp_path, monkeypatch)
    try:
        sample = tmp_path / "migration-sample.json"
        first = {"note": "first-frame"}
        sample.write_text(json.dumps(first), encoding="utf-8")
        scanner.ingest_message(json.dumps({"note": "second-frame"}))
        assert json.loads(sample.read_text(encoding="utf-8")) == first
        assert scanner.waiting == {}
    finally:
        db.close()


def test_mapped_frame_admits_only_configured_keys(tmp_path, monkeypatch):
    scanner, db, _lines = _scanner(tmp_path, monkeypatch)
    try:
        (tmp_path / "migration_keys.json").write_text(
            json.dumps(
                {
                    "mint": "fixtureMint",
                    "pool": "fixturePool",
                    "signature": "fixtureSig",
                    "creator": "fixtureCreator",
                }
            ),
            encoding="utf-8",
        )
        scanner.ingest_message(
            json.dumps(
                {
                    "txType": "migrate",
                    "fixtureMint": "fixture-mint",
                    "fixturePool": "pump-amm",
                    "fixtureSig": "fixture-sig",
                    "fixtureCreator": "fixture-creator",
                    "mint": "not-the-mapped-name",
                    "pool": "not-the-mapped-name",
                }
            )
        )
        assert list(scanner.waiting) == ["fixture-mint"]
        admitted = scanner.waiting["fixture-mint"]
        assert admitted["mint"] == "fixture-mint"
        assert admitted["pool"] is None
        assert admitted["pool_label"] == "pump-amm"
        assert admitted["signature"] == "fixture-sig"
        assert admitted["creator"] == "fixture-creator"
        assert scanner.bus.auditor.empty()
        row = db.coin("fixture-mint")
        assert row["pool"] is None
        assert row["creator"] == "fixture-creator"
    finally:
        db.close()
