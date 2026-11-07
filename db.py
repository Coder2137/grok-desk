"""SQLite schema (WAL) and the helpers the agents share."""

import sqlite3
from pathlib import Path

from config import defaults, iso, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    pool TEXT,
    migrated_at TEXT,
    creator TEXT,
    creator_balance_at_migration REAL,
    holders INTEGER
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    mint TEXT,
    ts TEXT,
    liquidity_usd REAL,
    top10_pct REAL,
    h1_change_pct REAL,
    age_min REAL,
    verdict TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY,
    mint TEXT,
    ts TEXT,
    layer INTEGER,
    name TEXT,
    value_text TEXT,
    pass INTEGER,
    source TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
    mint TEXT,
    ts TEXT,
    alive_6h REAL,
    organic REAL,
    flush REAL,
    chase REAL,
    score REAL,
    pass INTEGER,
    reason TEXT,
    input_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    mint TEXT,
    ts TEXT,
    price REAL,
    size_usd REAL,
    stack_pct REAL,
    link TEXT,
    mode TEXT,
    status TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    mint TEXT,
    opened_at TEXT,
    entry_price REAL,
    size_usd REAL,
    peak_price REAL,
    stop_price REAL,
    trail_active INTEGER,
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    fees_usd REAL,
    mode TEXT,
    stop_pct REAL,
    trail_start_pct REAL,
    trail_pct REAL,
    timeout_h REAL,
    timeout_min_gain_pct REAL,
    status TEXT,
    name TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    kind TEXT,
    mint TEXT,
    agent TEXT,
    title TEXT,
    subtitle TEXT,
    value TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    mint TEXT,
    ts TEXT,
    price REAL,
    volume_m5 REAL
);
CREATE TABLE IF NOT EXISTS balance (
    ts TEXT,
    value REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS change_log (
    ts TEXT,
    key TEXT,
    old TEXT,
    new TEXT,
    who TEXT
);
CREATE TABLE IF NOT EXISTS skips (
    mint TEXT,
    until TEXT
);
CREATE TABLE IF NOT EXISTS heartbeats (
    agent TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT,
    auth TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS runners (
    mint TEXT,
    name TEXT,
    symbol TEXT,
    day TEXT,
    h24 REAL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def ensure_defaults(self) -> None:
        now = iso(utc_now())
        for key, value in defaults().items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, _store(value), now),
            )
        self.conn.commit()

    def get_setting(self, key: str):
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return defaults().get(key)
        return row["value"]

    def set_setting(self, key: str, value, who: str) -> None:
        old = self.get_setting(key)
        now = iso(utc_now())
        stored = _store(value)
        self.conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, stored, now),
        )
        self.conn.execute(
            "INSERT INTO change_log (ts, key, old, new, who) VALUES (?, ?, ?, ?, ?)",
            (now, key, _store(old), stored, who),
        )
        self.conn.commit()

    def heartbeat(self, agent: str, moment: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO heartbeats (agent, ts) VALUES (?, ?)",
            (agent, moment or iso(utc_now())),
        )
        self.conn.commit()

    def latest_heartbeats(self) -> dict:
        rows = self.conn.execute(
            "SELECT agent, MAX(ts) AS ts FROM heartbeats GROUP BY agent"
        ).fetchall()
        return {row["agent"]: row["ts"] for row in rows}

    def upsert_coin(self, **fields) -> None:
        mint = fields["mint"]
        current = self.conn.execute("SELECT * FROM coins WHERE mint = ?", (mint,)).fetchone()
        if current is None:
            self.conn.execute(
                """
                INSERT INTO coins (mint, name, symbol, pool, migrated_at, creator,
                    creator_balance_at_migration, holders)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mint,
                    fields.get("name"),
                    fields.get("symbol"),
                    fields.get("pool"),
                    fields.get("migrated_at"),
                    fields.get("creator"),
                    fields.get("creator_balance_at_migration"),
                    fields.get("holders"),
                ),
            )
        else:
            for key in (
                "name",
                "symbol",
                "pool",
                "migrated_at",
                "creator",
                "creator_balance_at_migration",
                "holders",
            ):
                if key in fields and fields[key] is not None:
                    self.conn.execute(
                        f"UPDATE coins SET {key} = ? WHERE mint = ?",
                        (fields[key], mint),
                    )
        self.conn.commit()

    def coin(self, mint: str):
        return self.conn.execute("SELECT * FROM coins WHERE mint = ?", (mint,)).fetchone()

    def coins(self):
        return self.conn.execute("SELECT * FROM coins").fetchall()

    def add_scan(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO scans (mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason)
            VALUES (:mint, :ts, :liquidity_usd, :top10_pct, :h1_change_pct, :age_min, :verdict, :reason)
            """,
            row,
        )
        self.conn.commit()

    def scans_since(self, ts: str):
        return self.conn.execute(
            "SELECT * FROM scans WHERE ts >= ? ORDER BY id DESC",
            (ts,),
        ).fetchall()

    def latest_scan(self, mint: str):
        return self.conn.execute(
            "SELECT * FROM scans WHERE mint = ? ORDER BY id DESC LIMIT 1",
            (mint,),
        ).fetchone()

    def passed_tonight(self, mint: str, since: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM scans WHERE mint = ? AND ts >= ? AND verdict = 'pass' LIMIT 1",
            (mint, since),
        ).fetchone()
        return row is not None

    def add_price(self, mint: str, price, volume_m5, ts: str | None = None) -> None:
        if price is None:
            return
        self.conn.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, ?)",
            (mint, ts or iso(utc_now()), float(price), volume_m5),
        )
        self.conn.commit()

    def prices_for(self, mint: str, since: str | None = None):
        if since:
            return self.conn.execute(
                "SELECT * FROM prices WHERE mint = ? AND ts >= ? ORDER BY ts",
                (mint, since),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM prices WHERE mint = ? ORDER BY ts",
            (mint,),
        ).fetchall()

    def latest_price(self, mint: str):
        return self.conn.execute(
            "SELECT * FROM prices WHERE mint = ? ORDER BY ts DESC LIMIT 1",
            (mint,),
        ).fetchone()

    def add_balance(self, value: float, ts: str | None = None) -> None:
        moment = ts or iso(utc_now())
        minute = moment[:16]
        existing = self.conn.execute(
            "SELECT ts FROM balance WHERE substr(ts, 1, 16) = ? LIMIT 1",
            (minute,),
        ).fetchone()
        if existing:
            return
        self.conn.execute("INSERT INTO balance (ts, value) VALUES (?, ?)", (moment, float(value)))
        self.conn.commit()

    def balances_since(self, ts: str):
        return self.conn.execute(
            "SELECT * FROM balance WHERE ts >= ? ORDER BY ts",
            (ts,),
        ).fetchall()

    def add_audit_layer(self, mint, ts, layer, name, value_text, passed, source) -> None:
        self.conn.execute(
            """
            INSERT INTO audits (mint, ts, layer, name, value_text, pass, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, ts, layer, name, value_text, None if passed is None else int(bool(passed)), source),
        )
        self.conn.commit()

    def clear_audit(self, mint: str) -> None:
        self.conn.execute("DELETE FROM audits WHERE mint = ?", (mint,))
        self.conn.commit()

    def audits_for(self, mint: str):
        return self.conn.execute(
            "SELECT * FROM audits WHERE mint = ? ORDER BY id",
            (mint,),
        ).fetchall()

    def rejected_tonight(self, mint: str, since: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM audits
            WHERE mint = ? AND ts >= ? AND pass = 0 AND value_text != 'not checked'
            LIMIT 1
            """,
            (mint, since),
        ).fetchone()
        return row is not None

    def add_verdict(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO verdicts
            (mint, ts, alive_6h, organic, flush, chase, score, pass, reason, input_tokens)
            VALUES (:mint, :ts, :alive_6h, :organic, :flush, :chase, :score, :pass, :reason, :input_tokens)
            """,
            row,
        )
        self.conn.commit()

    def verdicts_since(self, ts: str):
        return self.conn.execute(
            "SELECT * FROM verdicts WHERE ts >= ? ORDER BY id DESC",
            (ts,),
        ).fetchall()

    def add_signal(self, row: dict) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO signals (mint, ts, price, size_usd, stack_pct, link, mode, status, reason)
            VALUES (:mint, :ts, :price, :size_usd, :stack_pct, :link, :mode, :status, :reason)
            """,
            row,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields) -> None:
        for key, value in fields.items():
            self.conn.execute(f"UPDATE signals SET {key} = ? WHERE id = ?", (value, signal_id))
        self.conn.commit()

    def signal_tonight(self, mint: str, since: str):
        return self.conn.execute(
            """
            SELECT * FROM signals
            WHERE mint = ? AND ts >= ? AND status IN ('sent', 'bought', 'missed')
            ORDER BY id DESC LIMIT 1
            """,
            (mint, since),
        ).fetchone()

    def signals_since(self, ts: str):
        return self.conn.execute(
            "SELECT * FROM signals WHERE ts >= ? ORDER BY id DESC",
            (ts,),
        ).fetchall()

    def add_position(self, row: dict) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
                closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode, stop_pct,
                trail_start_pct, trail_pct, timeout_h, timeout_min_gain_pct, status, name
            ) VALUES (
                :mint, :opened_at, :entry_price, :size_usd, :peak_price, :stop_price, :trail_active,
                :closed_at, :exit_price, :exit_reason, :pnl_usd, :fees_usd, :mode, :stop_pct,
                :trail_start_pct, :trail_pct, :timeout_h, :timeout_min_gain_pct, :status, :name
            )
            """,
            row,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_position(self, position_id: int, **fields) -> None:
        for key, value in fields.items():
            self.conn.execute(
                f"UPDATE positions SET {key} = ? WHERE id = ?",
                (value, position_id),
            )
        self.conn.commit()

    def position(self, position_id: int):
        return self.conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()

    def open_positions(self):
        return self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'exiting') ORDER BY id"
        ).fetchall()

    def open_position_for(self, mint: str):
        return self.conn.execute(
            """
            SELECT * FROM positions
            WHERE mint = ? AND status IN ('open', 'exiting')
            ORDER BY id DESC LIMIT 1
            """,
            (mint,),
        ).fetchone()

    def positions_since(self, ts: str):
        return self.conn.execute(
            "SELECT * FROM positions WHERE opened_at >= ? OR (closed_at IS NOT NULL AND closed_at >= ?) ORDER BY id",
            (ts, ts),
        ).fetchall()

    def add_activity(self, kind, mint, agent, title, subtitle, value, ts: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO activity (ts, kind, mint, agent, title, subtitle, value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts or iso(utc_now()), kind, mint, agent, title, subtitle, value),
        )
        self.conn.commit()

    def activity_since(self, ts: str, limit: int = 50):
        return self.conn.execute(
            "SELECT * FROM activity WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (ts, limit),
        ).fetchall()

    def latest_activity(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def add_skip(self, mint: str, until: str) -> None:
        self.conn.execute("DELETE FROM skips WHERE mint = ?", (mint,))
        self.conn.execute("INSERT INTO skips (mint, until) VALUES (?, ?)", (mint, until))
        self.conn.commit()

    def skipped(self, mint: str, now: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM skips WHERE mint = ? AND until > ? LIMIT 1",
            (mint, now),
        ).fetchone()
        return row is not None

    def clear_expired_skips(self, now: str) -> None:
        self.conn.execute("DELETE FROM skips WHERE until <= ?", (now,))
        self.conn.commit()

    def add_runner(self, mint, name, symbol, day, h24) -> None:
        self.conn.execute("DELETE FROM runners WHERE mint = ? AND day = ?", (mint, day))
        self.conn.execute(
            "INSERT INTO runners (mint, name, symbol, day, h24) VALUES (?, ?, ?, ?, ?)",
            (mint, name, symbol, day, h24),
        )
        self.conn.commit()

    def runners_for(self, day: str):
        return self.conn.execute("SELECT * FROM runners WHERE day = ?", (day,)).fetchall()

    def find_coins(self, text: str):
        needle = text.strip().lower()
        rows = self.coins()
        exact = []
        for row in rows:
            if row["mint"] and row["mint"].lower() == needle:
                exact.append(row)
            elif (row["symbol"] or "").lower() == needle or (row["name"] or "").lower() == needle:
                exact.append(row)
        return exact

    def audited_mints(self, since: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT mint FROM audits WHERE ts >= ?",
            (since,),
        ).fetchall()
        return [row["mint"] for row in rows]

    def pushed_mints(self, since: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT mint FROM signals WHERE ts >= ? AND status IN ('sent', 'bought', 'missed')",
            (since,),
        ).fetchall()
        return [row["mint"] for row in rows]


def _store(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)
