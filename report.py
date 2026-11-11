"""Morning page. Run this from cron. It is not part of the always-on process.

The page has no keys and no tokens. Coin links come from links.py.
"""

import os
from pathlib import Path

from config import DeskSettings, iso, utc_now
from db import Database
from instance import INSTANCES, apply_instance, display_name, resolve_instance, setting_overrides
from links import coin_url
from telegram import _equity


def write_report(db: Database, settings: DeskSettings, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    since = settings.get("desk.night_start") or "1970"
    mode = settings.get("desk.mode")
    equity = _equity(db, settings)
    rows = []
    for position in db.positions_since(since):
        name = position["name"] or position["mint"]
        link = coin_url(position["mint"], "solana")
        pnl = position["pnl_usd"]
        pnl_txt = "" if pnl is None else f"${float(pnl):.2f}"
        rows.append(
            f"<li>{_esc(name)} { _esc(position['exit_reason'] or position['status']) } {pnl_txt} "
            f"<a href=\"{_esc(link)}\">FOMO</a></li>"
        )
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morning</title>
<style>
body {{ margin: 0; background: #000; color: #f5f5f7; font: 16px/1.4 -apple-system, sans-serif; }}
main {{ max-width: 390px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 28px; margin: 0 0 8px; }}
p {{ color: #8e8e93; }}
a {{ color: #64d2ff; }}
</style>
</head>
<body>
<main>
<h1>{_esc(display_name())}</h1>
<p>Morning · {_esc(mode)} · ${equity:.2f} · { _esc(iso(utc_now())) }</p>
<ul>
{''.join(rows) or '<li>No trades</li>'}
</ul>
</main>
</body>
</html>
"""
    out.write_text(body, encoding="utf-8")
    return out


def _esc(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main() -> None:
    name = resolve_instance()
    apply_instance(name)
    db = Database(os.environ.get("DESK_DB", "var/desk.db"))
    settings = DeskSettings(db, overrides=setting_overrides(name))
    default_out = INSTANCES.get(name, INSTANCES["desk1"])["report"]
    out = Path(os.environ.get("REPORT_PATH", default_out))
    path = write_report(db, settings, out)
    print(path.name)


if __name__ == "__main__":
    main()
