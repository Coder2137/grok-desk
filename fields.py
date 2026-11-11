"""Raw key mapping. A missing key is null. Nothing here invents a key name.

The 2026-11-07 PumpPortal migration frame used mint, pool, signature, and
txType migrate. creator was absent. pool's value is a venue label such as
pump-amm, not an account address. A gitignored var/migration_keys.json may
override those names on the operator machine. That file is never committed.
"""

import json
from pathlib import Path


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()

# Names from the observed migration frame. creator was not in that frame.
# None means "do not read".
MIGRATION_KEYS = {
    "mint": "mint",
    "pool": "pool",
    "signature": "signature",
    "creator": None,
}

_MIGRATION_FILE = Path("var") / "migration_keys.json"


def load_migration_keys() -> dict:
    keys = dict(MIGRATION_KEYS)
    if _MIGRATION_FILE.is_file():
        try:
            raw = json.loads(_MIGRATION_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return keys
        if isinstance(raw, dict):
            for name in keys:
                value = raw.get(name)
                if isinstance(value, str) and value:
                    keys[name] = value
    return keys


def pick(obj, path: str):
    """Walk a dotted path. Missing key -> MISSING. Present JSON null -> None."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return MISSING
        cur = cur[part]
    return cur


def as_float(value):
    if value is MISSING or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_text(value):
    if value is MISSING or value is None:
        return None
    return str(value)


def migration_fields(message, keys=None) -> dict:
    """mint, pool label, signature, creator. Unmapped or absent -> None.

    pool is the venue label from the migration frame, not an account address.
    """
    use = keys or load_migration_keys()
    out = {"mint": None, "pool": None, "signature": None, "creator": None}
    if not isinstance(message, dict):
        return out
    for name in out:
        key = use.get(name)
        if not key or key not in message:
            out[name] = None
        else:
            value = message.get(key)
            out[name] = None if value is None else str(value)
    return out


def migration_frame(message, keys=None) -> dict | None:
    """Fields from a subscribeMigration data frame. None for any other message."""
    if not isinstance(message, dict) or message.get("txType") != "migrate":
        return None
    return migration_fields(message, keys)


def pumpswap_pair(payload):
    """DexScreener returns a list. Use dexId == pumpswap with the most liquidity.usd."""
    rows = payload if isinstance(payload, list) else None
    if rows is None:
        return None
    best = None
    best_liq = None
    for row in rows:
        if not isinstance(row, dict) or row.get("dexId") != "pumpswap":
            continue
        liq = as_float(pick(row, "liquidity.usd"))
        if best is None or (liq is not None and (best_liq is None or liq > best_liq)):
            best = row
            best_liq = liq
    return best


def dex_fields(pair) -> dict:
    names = {
        "name": "baseToken.name",
        "symbol": "baseToken.symbol",
        "mint": "baseToken.address",
        "pool": "pairAddress",
        "dex": "dexId",
        "priceUsd": "priceUsd",
        "liquidityUsd": "liquidity.usd",
        "marketCap": "marketCap",
        "volumeM5": "volume.m5",
        "volumeH1": "volume.h1",
        "volumeH24": "volume.h24",
        "txnsM5Buys": "txns.m5.buys",
        "txnsM5Sells": "txns.m5.sells",
        "txnsH1Buys": "txns.h1.buys",
        "txnsH1Sells": "txns.h1.sells",
        "priceChangeM5": "priceChange.m5",
        "priceChangeH1": "priceChange.h1",
        "priceChangeH24": "priceChange.h24",
        "pairCreatedAt": "pairCreatedAt",
    }
    out = {key: None for key in names}
    if not isinstance(pair, dict):
        return out
    for key, path in names.items():
        value = pick(pair, path)
        if key in ("name", "symbol", "mint", "pool", "dex"):
            out[key] = as_text(value)
        else:
            out[key] = as_float(value) if key != "pairCreatedAt" else (
                None if value is MISSING else value
            )
            if key == "pairCreatedAt":
                out[key] = None if value is MISSING else value
    return out


def rug_report_fields(report) -> dict:
    if not isinstance(report, dict):
        report = {}
    mint_auth = pick(report, "mintAuthority")
    if mint_auth is MISSING:
        mint_auth = pick(report, "token.mintAuthority")
    freeze_auth = pick(report, "freezeAuthority")
    if freeze_auth is MISSING:
        freeze_auth = pick(report, "token.freezeAuthority")
    return {
        "creator": as_text(pick(report, "creator")),
        "creatorBalance": as_float(pick(report, "creatorBalance")),
        "mintAuthority": None if mint_auth is MISSING else mint_auth,
        "mintAuthorityKnown": mint_auth is not MISSING,
        "freezeAuthority": None if freeze_auth is MISSING else freeze_auth,
        "freezeAuthorityKnown": freeze_auth is not MISSING,
        "topHolders": pick(report, "topHolders") if pick(report, "topHolders") is not MISSING else None,
        "totalHolders": as_float(pick(report, "totalHolders")),
    }


def _market_has_pool(market: dict, pool: str | None) -> bool:
    if not pool:
        return False
    if as_text(pick(market, "pubkey")) == pool:
        return True

    def walk(value) -> bool:
        if isinstance(value, str):
            return value == pool
        if isinstance(value, dict):
            return any(walk(item) for item in value.values())
        if isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    return walk(market)


def rug_lp(report, pool: str) -> dict:
    """Market whose pubkey equals the scanner pool. Burned % has no sample key."""
    markets = pick(report, "markets") if isinstance(report, dict) else MISSING
    if not isinstance(markets, list):
        return {"found": False, "locked": None, "burned": None}
    for market in markets:
        if not isinstance(market, dict):
            continue
        if not _market_has_pool(market, pool):
            continue
        locked = pick(market, "lp.lpLockedPct")
        # No burned key in the STEP 2 sample. Do not invent one.
        return {
            "found": True,
            "locked": as_float(locked),
            "burned": None,
        }
    return {"found": False, "locked": None, "burned": None}


def graph_nodes_links(payload):
    """nodes = [0].nodes. edges key is absent; [0].links is the edge list.

    Returns (nodes, links, links_key_missing, empty_network).
    """
    if payload is None:
        return None, None, True, True
    if isinstance(payload, list):
        if len(payload) == 0:
            return None, None, True, True
        root = payload[0]
    else:
        root = payload
    if not isinstance(root, dict):
        return None, None, True, True
    nodes = root["nodes"] if "nodes" in root else None
    if "links" not in root:
        return nodes if isinstance(nodes, list) else None, None, True, False
    links = root.get("links")
    if not isinstance(links, list):
        return nodes if isinstance(nodes, list) else None, None, True, False
    if isinstance(nodes, list) and len(nodes) == 0 and len(links) == 0:
        return nodes, links, False, True
    return nodes if isinstance(nodes, list) else [], links, False, False


def rpc_mint_account(response) -> dict:
    info_path = "result.value.data.parsed.info"
    info = pick(response, info_path)
    if not isinstance(info, dict):
        return {
            "mintAuthority": MISSING,
            "freezeAuthority": MISSING,
            "supply": None,
            "decimals": None,
        }
    supply = pick(info, "supply")
    decimals = pick(info, "decimals")
    return {
        "mintAuthority": pick(info, "mintAuthority"),
        "freezeAuthority": pick(info, "freezeAuthority"),
        "supply": as_text(supply),
        "decimals": as_float(decimals),
    }


def rpc_supply_ui(mint_account: dict):
    raw = mint_account.get("supply") if isinstance(mint_account, dict) else None
    decimals = mint_account.get("decimals") if isinstance(mint_account, dict) else None
    if raw is None or decimals is None:
        return None
    try:
        return int(raw) / (10 ** int(decimals))
    except (TypeError, ValueError, OverflowError):
        return None


def token_account_fields(account_obj) -> dict:
    """Mapped token-account keys. Delegate may be absent -> null."""
    if not isinstance(account_obj, dict):
        account_obj = {}
    info = pick(account_obj, "account.data.parsed.info")
    if not isinstance(info, dict):
        info = account_obj if "mint" in account_obj or "tokenAmount" in account_obj else {}
        base = info
        mint = pick(base, "mint")
        owner = pick(base, "owner")
        balance = pick(base, "tokenAmount.uiAmount")
        if balance is MISSING:
            balance = pick(base, "uiAmount")
        delegate = pick(base, "delegate")
    else:
        mint = pick(info, "mint")
        owner = pick(info, "owner")
        balance = pick(info, "tokenAmount.uiAmount")
        delegate = pick(info, "delegate")
    return {
        "mint": as_text(mint),
        "owner": as_text(owner),
        "balance": as_float(balance),
        "delegate": None if delegate is MISSING or delegate is None else as_text(delegate),
    }


def largest_account_rows(response) -> list | None:
    """getTokenLargestAccounts addresses. Amounts come from parsed token accounts."""
    value = pick(response, "result.value")
    if value is MISSING or value is None:
        return None
    if not isinstance(value, list):
        return None
    rows = []
    for item in value:
        if isinstance(item, dict) and "address" in item and item.get("address"):
            rows.append({"address": str(item["address"])})
    return rows


def program_account_rows(response) -> list | None:
    value = pick(response, "result.value")
    if value is MISSING or value is None:
        return None
    if not isinstance(value, list):
        return None
    return [token_account_fields(item) for item in value if isinstance(item, dict)]
