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
- `DATA_PATH` — 32 or more random characters. Plaintext JSON is written only under `var/json/d/<DATA_PATH>/`. The same bytes are encrypted into `var/live/`.
- `FOMO_HANDLE` — FOMO profile handle for the Account tab.
- `APP_TOKEN` — checked only if you start the optional local API.
- `TYPESAFE_API_KEY` — leave unset. Analyst stays in rules mode.
- `VAPID_PRIVATE_KEY` — unused. Telegram is the notifier.
- `RPC_URL` — optional. Default is the public Solana RPC.
- `DESK_DB` — optional sqlite path. Default `var/desk.db`.
- `JSON_ROOT` — optional. Default `var/json`.
- `LIVE_ROOT` — optional. Default `var/live`. Encrypted envelopes for the public phone site.
- `GH_TOKEN` — used only by `publish_live.py`. It needs permission to write the `live-data` branch and to dispatch the Pages workflow. `GITHUB_TOKEN` is read when `GH_TOKEN` is unset. Keep the token in the environment.
- `PAGES_URL` — optional public site, no secret path, used by the `report` command.
- `LOCAL_API=1` — optional loopback server for the phone UI and `/api/health`.

`python report.py` writes the morning page to `var/report/morning.html`. Run it on the morning schedule. It does not print secret values.

PumpPortal migration field names are unset until a live frame is saved in `logs/raw/` (gitignored). Map `mint`, `pool`, `signature`, and `creator` in `var/migration_keys.json` on the operator machine. That file stays out of git.

## Phone

GitHub Pages serves `web/` at the repository Pages URL. Open it once as `{pages url}#d=` plus the secret path. The app stores that in localStorage. Every 15 seconds it requests `d/<path>/state.json`, `chart.json`, `activity.json`, and `coin/<mint>.json`. If a file comes back as JSON, the app uses it. If that request 404s, the app fetches the same name under `live/` and decrypts it in the browser. The key is the SHA-256 of the stored path, and the additional data is the file name (`state.json`, `chart.json`, `activity.json`, or `coin/<mint>.json`). Settings, pause, and mode are changed in Telegram. The Account tab is read-only.

Do not commit a `d/` directory. A request for bare `/d/` 404s. The public `live/` files are ciphertext envelopes (`v`, `alg`, `n`, `c`) with fixed names. They do not contain the path.

FOMO URLs are built only in `links.py`.

## Live branch

The writer stores those envelopes under `var/live/` (or `LIVE_ROOT`). That directory stays on the operator machine.

```bash
python publish_live.py
```

The script reads `GH_TOKEN` or `GITHUB_TOKEN` from the environment and updates the `live-data` branch so the branch contains only the encrypted files. It creates the branch when it is missing, then dispatches the Pages workflow. Run it about once a minute. A commit to `main` is not required for each refresh. Set `GITHUB_REPOSITORY` to `owner/name` when the checkout has no `origin` remote.

The token needs permission to write the `live-data` branch and to dispatch workflows on this repository. Keep it in the operator environment.

GitHub Actions checks out `web/` from `main`, copies the `live-data` branch into `web/live/` when that branch exists, and deploys that site. The first deploy after merge already includes the encrypted snapshot in `web/live/`.

## Tests

```bash
python -m pytest
```
