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
- `GH_TOKEN` — used only by `publish_live.py`. It needs permission to push the `live-data` and `pages-src` branches. `GITHUB_TOKEN` is read when `GH_TOKEN` is unset. Keep the token in the environment.
- `PAGES_URL` — optional public site, no secret path, used by the `report` command.
- `LOCAL_API=1` — optional loopback server for the phone UI and `/api/health`.

`python report.py` writes the morning page to `var/report/morning.html`. Run it on the morning schedule. It does not print secret values.

A PumpPortal migration frame uses `mint`, `signature`, and `pool`. On the captured frame `pool` was the venue label `pump-amm`, not an account address, and `creator` was absent so it stays null. The account address used later is the DexScreener pair address when that pair is known. The subscribe reply `Subscribed to 'migration' events.` is not a migration frame. `var/migration_keys.json` on the operator machine can override those names. That file stays out of git.

## Phone

GitHub Pages serves `web/` at the repository Pages URL. Open it once as `{pages url}#d=` plus the secret path. The app stores that in localStorage. Every 15 seconds it requests `d/<path>/state.json`, `chart.json`, `activity.json`, and `coin/<mint>.json`. A scanner rejection still gets a coin file: the scan verdict and reason are in it, and audit fields that were not measured stay null. If a file comes back as JSON, the app uses it. If that request 404s, the app fetches the same name under `live/` and decrypts it in the browser. The key is the SHA-256 of the stored path, and the additional data is the file name (`state.json`, `chart.json`, `activity.json`, or `coin/<mint>.json`). Settings, pause, and mode are changed in Telegram. The Account tab is read-only.

Do not commit a `d/` directory. A request for bare `/d/` 404s. The public `live/` files are ciphertext envelopes (`v`, `alg`, `n`, `c`) with fixed names. They do not contain the path.

FOMO URLs are built only in `links.py`.

## Live branch

The writer stores those envelopes under `var/live/` (or `LIVE_ROOT`). That directory stays on the operator machine.

```bash
python publish_live.py
```

The script reads `GH_TOKEN` or `GITHUB_TOKEN` from the environment. It updates `live-data` with a commit based on that branch's current tree, uploading only envelopes whose blob changed. Unchanged coin files stay on the branch and are not sent again. A file removed locally is left in place. It then adds a `pages-src` commit the same way: the first commit starts from `main`, and later commits keep the `pages-src` tree and replace only the `web/live` blobs that changed. Pushing `pages-src` runs the Pages workflow. `actions/deploy-pages` sends that commit's SHA as `pages_build_version`, which is the version GitHub Pages stores. Run the script about once a minute. A commit to `main` is not required for each refresh. Set `GITHUB_REPOSITORY` to `owner/name` when the checkout has no `origin` remote.

The token needs permission to push `live-data` and `pages-src`. It does not need permission to dispatch workflows. Keep it in the operator environment. Dispatching the Pages workflow on `main` reuses `main`'s SHA. A quiet dispatch of that kind uploaded a new artifact and reported success while the public site kept serving the older artifact for that same SHA. The script does not do that.

On a push to `pages-src`, Actions checks out that commit and deploys it. On a push to `main`, Actions checks out that commit and copies the current `live-data` branch into `web/live/` before deploying. The first deploy after merge already includes the encrypted snapshot in `web/live/`.

## Fees

PAPER PnL estimates Solana trading fees from the fomo help page "Trading fees on fomo" (article 14436214). The default `desk.fee_route` is `lowest`: 0.10 USDC under 5, 2% from 5 to 47.50, 0.95 USDC from 47.50 to 190, and 0.50% from 190 up. The fee is rounded half-up to cents. Solana gas, priority fees, and token rent stay 0 in the estimate because fomo covers them. No referral discount is applied. The route is chosen when you tap Buy or Sell. This process never taps, so the number is an estimate of that schedule.

`conservative` uses the other Solana routes: 0.50% with a 0.95 USDC minimum. Under 5 USDC the lowest route is 0.10 and the other routes are 0.95, so a small PAPER sell on the default can be cheaper than the route that is actually available. After half-up cent rounding the schedules match from 47.25 USDC up. The published unrounded bands meet at 47.50. The default stays `lowest` until you set `fee_route` to `conservative`. The 47.50 ticket floor is this desk's minimum, not a fomo platform minimum.

A full close fees the entry notional and the exit notional. Risk closes the whole position. A partial-exit estimate, `paper_pnl(..., sold_usd=...)`, fees only that slice: a pro-rata share of the entry fee, plus the sell fee on the slice proceeds.

## Tests

```bash
python -m pytest
```
