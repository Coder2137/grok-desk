# Grok Bot desk

Private memecoin desk for one operator. It watches PumpSwap graduations, audits them, scores them, and sizes a paper ticket. FOMO has no order API here. The process never places an order, never asks for a FOMO password, and never moves funds. Buy and Sell open a FOMO page. You tap.

Python 3.13 on the operator machine. The code also runs on 3.12.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Set these in the environment or in a gitignored `.env`. Do not commit the values.

- `TG_TOKEN` — Telegram bot token. The first `/start` becomes the owner.
- `DATA_PATH` — 32 or more random characters. JSON is written only under `var/json/d/<DATA_PATH>/`.
- `FOMO_HANDLE` — FOMO profile handle for the Account tab.
- `APP_TOKEN` — checked only if you start the optional local API.
- `TYPESAFE_API_KEY` — leave unset. Analyst stays in rules mode.
- `VAPID_PRIVATE_KEY` — unused. Telegram is the notifier.
- `RPC_URL` — optional. Default is the public Solana RPC.
- `DESK_DB` — optional sqlite path. Default `var/desk.db`.
- `JSON_ROOT` — optional. Default `var/json`.
- `PAGES_URL` — optional public site, no secret path, used by the `report` command.
- `LOCAL_API=1` — optional loopback server for the phone UI and `/api/health`.

`python report.py` writes the morning page to `var/report/morning.html`. Run it on the morning schedule. It does not print secret values.

PumpPortal migration field names are unset until a live frame is saved in `logs/raw/` (gitignored). Map `mint`, `pool`, `signature`, and `creator` in `var/migration_keys.json` on the operator machine. That file stays out of git.

## Phone

GitHub Pages serves `web/` at the repository Pages URL. Open it once as `{pages url}#d=` plus the secret path. The app stores that in localStorage and polls `d/<path>/state.json`, `chart.json`, `activity.json`, and `coin/<mint>.json` every 15 seconds. Settings, pause, and mode are changed in Telegram. The Account tab is read-only.

Do not commit a `d/` directory. A request for bare `/d/` 404s.

FOMO URLs are built only in `links.py`.

## Tests

```bash
python -m pytest
```
