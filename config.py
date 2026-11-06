"""Desk settings: STEP 10 defaults, ranges, and the ticket check."""

from datetime import datetime, timedelta, timezone

# key, kind, default, minimum, maximum
# kind: float, int, bool, enum, time, text, url
SETTINGS = {
    "scanner.min_liquidity_usd": ("float", 40000, 0, 10_000_000),
    "scanner.max_top10_pct": ("float", 25, 1, 100),
    "scanner.max_h1_change_pct": ("float", 80, 1, 10000),
    "scanner.poll_s": ("int", 60, 5, 600),
    "scanner.min_pool_age_min": ("float", 10, 0, 1440),
    "scanner.max_wait_min": ("float", 60, 1, 1440),
    "scanner.runner_h24_pct": ("float", 300, 0, 100000),
    "auditor.enabled": ("bool", True, None, None),
    "auditor.max_dev_pct": ("float", 5, 0, 100),
    "auditor.max_top10_pct": ("float", 25, 1, 100),
    "auditor.min_lp_locked_pct": ("float", 99, 0, 100),
    "auditor.min_pool_age_min": ("float", 10, 0, 1440),
    "auditor.max_bundled": ("int", 5, 1, 1000),
    "auditor.max_delegated_accounts": ("int", 10, 1, 10000),
    "auditor.retries": ("int", 3, 1, 10),
    "analyst.mode": ("enum", "rules", ("jev", "rules"), None),
    "analyst.min_alive_6h": ("float", 0.70, 0, 1),
    "analyst.min_organic": ("float", 0.65, 0, 1),
    "analyst.max_chase": ("float", 0.30, 0, 1),
    "analyst.min_score": ("float", 70, 0, 100),
    "analyst.timeout_s": ("int", 10, 1, 120),
    "rules.alive_volume_h1": ("float", 25000, 1, 1_000_000_000),
    "rules.organic_buys_h1": ("float", 150, 1, 10_000_000),
    "rules.bundled_penalty": ("float", 0.5, 0, 1),
    "rules.flush_drop_m5_pct": ("float", 5, 0, 100),
    "rules.chase_h1_pct": ("float", 80, 1, 10000),
    "executor.stack_pct": ("float", 8, 0.1, 100),
    "executor.max_pool_pct": ("float", 2, 0.01, 100),
    "executor.min_ticket_usd": ("float", 47.50, 0.01, 100000),
    "executor.below_min": ("enum", "bump", ("bump", "skip"), None),
    "executor.recheck_liq_drop_pct": ("float", 20, 0, 100),
    "executor.recheck_price_up_pct": ("float", 15, 0, 1000),
    "executor.confirm_window_min": ("int", 5, 1, 120),
    "risk.poll_s": ("int", 15, 5, 300),
    "risk.stop_pct": ("float", 6, 0.1, 90),
    "risk.trail_start_pct": ("float", 20, 0.1, 1000),
    "risk.trail_pct": ("float", 12, 0.1, 90),
    "risk.timeout_h": ("float", 3, 0.1, 168),
    "risk.timeout_min_gain_pct": ("float", 5, 0, 1000),
    "risk.confirm_window_min": ("int", 2, 1, 60),
    "desk.mode": ("enum", "PAPER", ("PAPER", "LIVE"), None),
    "desk.paper_start_stack": ("float", 200, 1, 1_000_000_000),
    "desk.stack": ("float", 200, 1, 1_000_000_000),
    "desk.report_time": ("time", "08:00", None, None),
    "desk.notifier": ("enum", "telegram", ("telegram",), None),
    "desk.fee_route": ("enum", "lowest", ("lowest", "conservative"), None),
    "desk.rpc_url": ("url", "https://api.mainnet-beta.solana.com", None, None),
    "desk.push_rejects": ("bool", False, None, None),
    "desk.fomo_handle": ("text", "", None, None),
    "desk.paper_nights": ("int", 0, 0, 100000),
    "desk.night_start": ("text", "", None, None),
    "desk.night_start_equity": ("float", 200, 0, 1_000_000_000),
    "desk.owner_chat_id": ("text", "", None, None),
    "desk.layer8_down": ("bool", False, None, None),
    "agent.scanner.paused": ("bool", False, None, None),
    "agent.auditor.paused": ("bool", False, None, None),
    "agent.analyst.paused": ("bool", False, None, None),
    "agent.executor.paused": ("bool", False, None, None),
    "agent.risk.paused": ("bool", False, None, None),
}

ALIASES = {
    "stop": "risk.stop_pct",
    "stop_pct": "risk.stop_pct",
    "trail": "risk.trail_pct",
    "trail_pct": "risk.trail_pct",
    "trail start": "risk.trail_start_pct",
    "trail_start": "risk.trail_start_pct",
    "timeout": "risk.timeout_h",
    "timeout_h": "risk.timeout_h",
    "stack_pct": "executor.stack_pct",
    "min_ticket": "executor.min_ticket_usd",
    "min_ticket_usd": "executor.min_ticket_usd",
    "below_min": "executor.below_min",
    "liquidity": "scanner.min_liquidity_usd",
    "min_liquidity": "scanner.min_liquidity_usd",
    "min_liquidity_usd": "scanner.min_liquidity_usd",
    "top10": "scanner.max_top10_pct",
    "mode": "analyst.mode",
    "analyst_mode": "analyst.mode",
    "notifier": "desk.notifier",
    "fee_route": "desk.fee_route",
    "fee route": "desk.fee_route",
    "rpc": "desk.rpc_url",
    "rpc_url": "desk.rpc_url",
    "fomo_handle": "desk.fomo_handle",
    "handle": "desk.fomo_handle",
    "paper_start_stack": "desk.paper_start_stack",
    "report_time": "desk.report_time",
    "push_rejects": "desk.push_rejects",
    "auditor": "auditor.enabled",
    "auditor.enabled": "auditor.enabled",
}

PERCENT_KEYS = {
    "risk.stop_pct",
    "risk.trail_pct",
    "risk.trail_start_pct",
    "risk.timeout_min_gain_pct",
    "executor.stack_pct",
    "executor.max_pool_pct",
    "scanner.max_top10_pct",
    "scanner.max_h1_change_pct",
    "auditor.max_dev_pct",
    "auditor.max_top10_pct",
    "auditor.min_lp_locked_pct",
    "executor.recheck_liq_drop_pct",
    "executor.recheck_price_up_pct",
}

GROUPS = {
    "scanner": "Scanner",
    "auditor": "Auditor",
    "analyst": "Analyst",
    "rules": "Rules",
    "executor": "Executor",
    "risk": "Risk",
    "desk": "Desk",
    "agent": "Agents",
}


def defaults() -> dict:
    return {key: spec[1] for key, spec in SETTINGS.items()}


def _kind(key: str):
    spec = SETTINGS.get(key)
    if not spec:
        return None
    return spec[0]


def coerce(key: str, raw):
    spec = SETTINGS.get(key)
    if not spec:
        return raw
    kind, default, _lo, _hi = spec
    if raw is None or raw == "":
        return default
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "enum":
        return str(raw).strip()
    return str(raw).strip()


def resolve_key(name: str) -> str | None:
    text = (name or "").strip()
    if text in SETTINGS:
        return text
    low = text.lower().replace("_", " ")
    if low in ALIASES:
        return ALIASES[low]
    compact = text.lower()
    if compact in ALIASES:
        return ALIASES[compact]
    return None


def validate(key: str, raw) -> tuple[bool, str, object]:
    spec = SETTINGS.get(key)
    if not spec:
        return False, f"unknown setting {key}", None
    kind, _default, lo, hi = spec
    try:
        if kind == "bool":
            value = coerce(key, raw)
        elif kind == "int":
            value = int(float(str(raw).replace("%", "").replace("+", "")))
        elif kind == "float":
            text = str(raw).strip().replace("%", "").replace("+", "")
            if text.startswith("-") and key in PERCENT_KEYS:
                text = text[1:]
            value = float(text)
        elif kind == "enum":
            value = str(raw).strip()
            allowed = lo
            if key == "desk.mode":
                value = value.upper()
            if key == "analyst.mode":
                value = value.lower()
            if key == "executor.below_min":
                value = value.lower()
            if key == "desk.fee_route":
                value = value.lower()
            if value not in allowed:
                return False, f"{key} must be one of {'|'.join(allowed)}", None
        elif kind == "time":
            value = str(raw).strip()
            hour, minute = value.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59 and len(minute) == 2):
                raise ValueError
            value = f"{int(hour):02d}:{int(minute):02d}"
        elif kind == "url":
            value = str(raw).strip()
            if not (value.startswith("https://") or value.startswith("http://")):
                return False, f"{key} must start with http:// or https://", None
            if " " in value:
                return False, f"{key} must not contain spaces", None
        else:
            value = str(raw).strip().lstrip("@")
            if key == "desk.fomo_handle" and value and not value.replace("_", "").isalnum():
                return False, "fomo_handle must be letters, numbers, or underscore", None
            if key == "desk.fomo_handle" and len(value) > 32:
                return False, "fomo_handle must be 32 characters or fewer", None
    except (TypeError, ValueError):
        return False, f"{key} is not a valid {kind}", None
    if kind in ("int", "float") and lo is not None and hi is not None:
        if value < lo or value > hi:
            return False, f"{key} must be between {lo} and {hi}", None
    return True, "", value


def format_value(key: str, value) -> str:
    if key in PERCENT_KEYS:
        number = float(value)
        text = str(int(number)) if number == int(number) else f"{number:g}"
        sign = "-" if key == "risk.stop_pct" else ""
        return f"{sign}{text}%"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def setting_label(key: str) -> str:
    short = {
        "risk.stop_pct": "stop",
        "risk.trail_pct": "trail",
        "executor.stack_pct": "stack %",
    }
    return short.get(key, key)


def min_stack(min_ticket_usd: float, stack_pct: float) -> float:
    if stack_pct <= 0:
        return float("inf")
    return float(min_ticket_usd) / (float(stack_pct) / 100.0)


def ticket_check(stack: float, stack_pct: float, min_ticket_usd: float, below_min: str) -> str | None:
    """Warn when the stack cannot fund the minimum ticket. None means the night can run."""
    needed = min_stack(min_ticket_usd, stack_pct)
    if stack >= needed:
        return None
    ticket = f"{float(min_ticket_usd):.2f}"
    if below_min == "bump":
        pct = (float(min_ticket_usd) / float(stack)) * 100 if stack else 0
        pct_txt = f"{pct:.2f}".rstrip("0").rstrip(".")
        return f"tickets bumped to ${ticket} = {pct_txt}% of stack"
    needed_txt = f"{needed:.2f}".rstrip("0").rstrip(".")
    if "." not in f"{needed:.2f}" or needed == int(needed):
        needed_txt = f"{needed:.2f}"
    needed_txt = f"{needed:.2f}"
    return f"every coin will be skipped, stack under ${needed_txt}"


def night_window(now: datetime, report_time: str) -> tuple[datetime, datetime]:
    hour, minute = report_time.split(":")
    start = now.astimezone(timezone.utc).replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0
    )
    if now.astimezone(timezone.utc) < start:
        start = start - timedelta(days=1)
    return start, start + timedelta(days=1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


class DeskSettings:
    """Live settings stored in SQLite. Env seeds the handle once, never a token."""

    def __init__(self, db, environ=None) -> None:
        import os

        self.db = db
        self.db.ensure_defaults()
        env = environ if environ is not None else os.environ
        if env.get("FOMO_HANDLE") and not self.db.get_setting("desk.fomo_handle"):
            ok, _msg, value = validate("desk.fomo_handle", env["FOMO_HANDLE"])
            if ok:
                self.db.set_setting("desk.fomo_handle", value, "env")
        if env.get("RPC_URL"):
            ok, _msg, value = validate("desk.rpc_url", env["RPC_URL"])
            if ok and self.db.get_setting("desk.rpc_url") == SETTINGS["desk.rpc_url"][1]:
                self.db.set_setting("desk.rpc_url", value, "env")
        if not self.get("desk.night_start"):
            self.roll_night(utc_now(), announce=False)

    def get(self, key: str):
        return coerce(key, self.db.get_setting(key))

    def all(self) -> dict:
        return {key: self.get(key) for key in SETTINGS}

    def set(self, key: str, raw, who: str) -> tuple[bool, str]:
        ok, message, value = validate(key, raw)
        if not ok:
            return False, message
        if key == "analyst.mode" and value == "jev":
            import os

            if not (os.environ.get("TYPESAFE_API_KEY") or "").strip():
                return False, "analyst mode stays rules until TYPESAFE_API_KEY is set"
        if key == "desk.mode" and value == "LIVE":
            if str(raw).strip() != "LIVE":
                return False, "type LIVE to leave PAPER"
            if int(self.get("desk.paper_nights")) < 1:
                return False, "need 1 full PAPER night"
        old = self.get(key)
        self.db.set_setting(key, value, who)
        reply = f"{setting_label(key)} {format_value(key, old)} -> {format_value(key, value)}"
        if key in ("desk.stack", "executor.stack_pct", "executor.below_min", "executor.min_ticket_usd"):
            note = self.ticket_note()
            if note:
                reply = f"{reply}. {note}"
        return True, reply

    def ticket_note(self) -> str | None:
        return ticket_check(
            float(self.get("desk.stack")),
            float(self.get("executor.stack_pct")),
            float(self.get("executor.min_ticket_usd")),
            self.get("executor.below_min"),
        )

    def night_bounds(self):
        return night_window(utc_now(), self.get("desk.report_time"))

    def roll_night(self, now: datetime, announce: bool = True) -> str | None:
        start, _end = night_window(now, self.get("desk.report_time"))
        current = self.get("desk.night_start")
        start_iso = iso(start)
        if current == start_iso:
            return None
        if current and self.get("desk.mode") == "PAPER":
            self.db.set_setting("desk.paper_nights", int(self.get("desk.paper_nights")) + 1, "night")
        equity = float(self.get("desk.paper_start_stack") if self.get("desk.mode") == "PAPER" else self.get("desk.stack"))
        self.db.set_setting("desk.night_start", start_iso, "night")
        self.db.set_setting("desk.night_start_equity", equity, "night")
        self.db.clear_expired_skips(iso(now))
        note = self.ticket_note()
        if announce:
            return note
        return note


def parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
