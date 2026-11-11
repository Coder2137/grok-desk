"""Which paper desk this process is. Desk 1 stays the default."""

import os
import sys

DESK1 = "desk1"
DESK2 = "desk2"
DESK2_LIQUIDITY = 10000


def resolve_instance(argv: list[str] | None = None, environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    args = list(sys.argv if argv is None else argv)
    name = (env.get("DESK_INSTANCE") or DESK1).strip().lower()
    if "--instance" in args:
        index = args.index("--instance")
        if index + 1 >= len(args):
            raise SystemExit("Use --instance desk1 or --instance desk2.")
        name = args[index + 1].strip().lower()
    if name not in (DESK1, DESK2):
        raise SystemExit("DESK_INSTANCE must be desk1 or desk2.")
    return name


def apply_instance(name: str, environ: dict | None = None) -> None:
    """Point desk 2 at its own files. Desk 1 is left untouched.

    A shared .env that still says var/desk.db or var/json does not pull desk 2
    onto desk 1's database or plaintext. Set DESK_DB or JSON_ROOT to some other
    path when you want a different desk 2 location.
    """
    if name != DESK2:
        return
    env = environ if environ is not None else os.environ
    if env.get("DESK_DB", "var/desk.db") in ("", "var/desk.db"):
        env["DESK_DB"] = "var/desk2.db"
    if env.get("JSON_ROOT", "var/json") in ("", "var/json"):
        env["JSON_ROOT"] = "var/desk2/json"
    env.setdefault("LOG_DIR", "logs/desk2")
    env.setdefault("DESK_LIVE_PREFIX", "desk2")
    env.setdefault("DESK_TELEGRAM", "0")
    env.setdefault("DESK_FEED_OFFSET", "var/desk2/feed.offset")
    env.setdefault("LOCAL_PORT", "8766")


def setting_overrides(name: str) -> dict | None:
    if name != DESK2:
        return None
    return {"scanner.min_liquidity_usd": DESK2_LIQUIDITY}
