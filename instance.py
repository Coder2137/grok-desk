"""Which paper desk this process is. Desk 1 stays the default.

Internal ids stay desk1, desk2, and desk3. Display names are the liquidity floors.
"""

import os
import sys

DESK1 = "desk1"
DESK2 = "desk2"
DESK3 = "desk3"

# id, files, and the scanner liquidity floor. Desk 1 uses the settings default.
INSTANCES = {
    DESK1: {
        "label": "Desk 40k",
        "liquidity": None,
        "db": "var/desk.db",
        "json_root": "var/json",
        "log_dir": "logs",
        "live_prefix": "",
        "feed_offset": "",
        "local_port": "8765",
        "report": "var/report/morning.html",
        "socket": True,
        "secrets": False,
        "telegram_off": False,
    },
    DESK2: {
        "label": "Desk 10k",
        "liquidity": 10000,
        "db": "var/desk2.db",
        "json_root": "var/desk2/json",
        "log_dir": "logs/desk2",
        "live_prefix": "desk2",
        "feed_offset": "var/desk2/feed.offset",
        "local_port": "8766",
        "report": "var/desk2/morning.html",
        "socket": False,
        "secrets": True,
        "telegram_off": True,
    },
    DESK3: {
        "label": "Desk 20k",
        "liquidity": 20000,
        "db": "var/desk3.db",
        "json_root": "var/desk3/json",
        "log_dir": "logs/desk3",
        "live_prefix": "desk3",
        "feed_offset": "var/desk3/feed.offset",
        "local_port": "8767",
        "report": "var/desk3/morning.html",
        "socket": False,
        "secrets": True,
        "telegram_off": True,
    },
}

DESK2_LIQUIDITY = INSTANCES[DESK2]["liquidity"]
DESK3_LIQUIDITY = INSTANCES[DESK3]["liquidity"]
LIVE_PREFIXES = tuple(spec["live_prefix"] for spec in INSTANCES.values() if spec["live_prefix"])


def resolve_instance(argv: list[str] | None = None, environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    args = list(sys.argv if argv is None else argv)
    name = (env.get("DESK_INSTANCE") or DESK1).strip().lower()
    if "--instance" in args:
        index = args.index("--instance")
        if index + 1 >= len(args):
            raise SystemExit("Use --instance desk1, desk2, or desk3.")
        name = args[index + 1].strip().lower()
    if name not in INSTANCES:
        raise SystemExit("DESK_INSTANCE must be desk1, desk2, or desk3.")
    return name


def display_name(name: str | None = None) -> str:
    key = (name or os.environ.get("DESK_INSTANCE") or DESK1).strip().lower()
    spec = INSTANCES.get(key) or INSTANCES[DESK1]
    return spec["label"]


def apply_instance(name: str, environ: dict | None = None) -> None:
    """Point a side desk at its own files. Desk 1 is left untouched.

    A shared .env that still says var/desk.db or var/json does not pull a side
    desk onto desk 1's database or plaintext.
    """
    spec = INSTANCES.get(name)
    if spec is None or name == DESK1:
        return
    env = environ if environ is not None else os.environ
    if env.get("DESK_DB", "var/desk.db") in ("", "var/desk.db"):
        env["DESK_DB"] = spec["db"]
    if env.get("JSON_ROOT", "var/json") in ("", "var/json"):
        env["JSON_ROOT"] = spec["json_root"]
    env["DESK_INSTANCE"] = name
    env.setdefault("LOG_DIR", spec["log_dir"])
    env.setdefault("DESK_LIVE_PREFIX", spec["live_prefix"])
    if spec["telegram_off"]:
        env["DESK_TELEGRAM"] = "0"
    if spec["feed_offset"]:
        env.setdefault("DESK_FEED_OFFSET", spec["feed_offset"])
    env.setdefault("LOCAL_PORT", spec["local_port"])


def setting_overrides(name: str) -> dict | None:
    spec = INSTANCES.get(name)
    if spec is None or spec["liquidity"] is None:
        return None
    return {"scanner.min_liquidity_usd": spec["liquidity"]}


def holds_socket(name: str) -> bool:
    spec = INSTANCES.get(name) or INSTANCES[DESK1]
    return bool(spec["socket"])


def loads_secrets(name: str) -> bool:
    spec = INSTANCES.get(name)
    return bool(spec and spec["secrets"])
