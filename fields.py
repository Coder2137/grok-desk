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
    A list of networks still exposes the first one here. insider_bundle_size
    reads every network.
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


def _bundle_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def insider_bundle_size(graph, report) -> tuple[int | None, str | None]:
    """Largest RugCheck insider network, in wallets.

    The graph is a list of networks. Each network's node count is the bundle.
    insiderNetworks[].size on the report is the same number and still counts
    when the first network has no links list. None means the source did not
    answer, which must fail closed.
    """
    sizes: list[int] = []
    if isinstance(report, dict) and isinstance(report.get("insiderNetworks"), list):
        for net in report["insiderNetworks"]:
            if not isinstance(net, dict):
                continue
            counted = _bundle_count(net.get("size"))
            if counted is None and "size" not in net:
                counted = _bundle_count(net.get("activeAccounts"))
            if counted is not None:
                sizes.append(counted)

    networks: list[dict] = []
    if isinstance(graph, list):
        networks = [item for item in graph if isinstance(item, dict)]
    elif isinstance(graph, dict):
        networks = [graph]

    structured = False
    for net in networks:
        nodes = net.get("nodes") if isinstance(net.get("nodes"), list) else None
        if nodes is not None or "links" in net or "size" in net:
            structured = True
        if nodes:
            ids = [node.get("id") for node in nodes if isinstance(node, dict) and node.get("id") is not None]
            sizes.append(len(set(ids)) if ids else len(nodes))
        else:
            counted = _bundle_count(net.get("size")) if "size" in net else None
            if counted is not None:
                sizes.append(counted)
    if sizes:
        return max(sizes), None
    # A positive detection count is a measurement. Zero beside an empty network
    # list is not: RugCheck leaves that list null until it has built the graph.
    if isinstance(report, dict):
        detected = _bundle_count(report.get("graphInsidersDetected"))
        if detected is not None and detected > 0:
            return detected, None
    if structured:
        return 0, None
    if graph is None and (not isinstance(report, dict) or "insiderNetworks" not in report):
        return None, "rugcheck unavailable"
    if isinstance(graph, list) and not graph:
        return None, "empty network"
    if isinstance(report, dict) and report.get("insiderNetworks") in (None, []):
        return None, "empty network"
    if graph is not None:
        return None, "links missing"
    return None, "rugcheck unavailable"


def _ui_amount(row: dict):
    amount = row.get("uiTokenAmount") if isinstance(row, dict) else None
    if not isinstance(amount, dict):
        return None
    ui = amount.get("uiAmount")
    if isinstance(ui, bool) or not isinstance(ui, (int, float)):
        return None
    return float(ui)


def buyers_from_transaction(tx, mint: str, pool: str | None) -> list[str] | None:
    """Wallets whose balance of mint increased. None when the transaction is unreadable."""
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta")
    if not isinstance(meta, dict) and isinstance(tx.get("result"), dict):
        meta = tx["result"].get("meta")
    if not isinstance(meta, dict):
        return None
    pre: dict[str, float] = {}
    post: dict[str, float] = {}
    for label, sink in (("preTokenBalances", pre), ("postTokenBalances", post)):
        rows = meta.get(label)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("mint") != mint:
                continue
            owner = row.get("owner")
            ui = _ui_amount(row)
            if not isinstance(owner, str) or ui is None:
                continue
            sink[owner] = sink.get(owner, 0.0) + ui
    buyers = []
    for owner in set(pre) | set(post):
        if pool and owner == pool:
            continue
        if post.get(owner, 0.0) - pre.get(owner, 0.0) > 1:
            buyers.append(owner)
    return buyers


def _transfer_source(instruction, buyers: set[str]) -> str | None:
    if not isinstance(instruction, dict):
        return None
    parsed = instruction.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
        return None
    info = parsed.get("info")
    if not isinstance(info, dict):
        return None
    source = info.get("source")
    destination = info.get("destination")
    if isinstance(source, str) and isinstance(destination, str) and destination in buyers and source not in buyers:
        return source
    return None


def funder_from_transaction(tx, buyers: list[str]) -> str | None:
    """A wallet that paid one of the buyers inside this transaction, when the transfer is parsed."""
    if not buyers or not isinstance(tx, dict):
        return None
    result = tx.get("result") if isinstance(tx.get("result"), dict) else tx
    message = ((result.get("transaction") or {}).get("message") or {}) if isinstance(result, dict) else {}
    meta = result.get("meta") if isinstance(result, dict) else None
    wanted = set(buyers)
    instructions = list(message.get("instructions") or [])
    for inner in (meta or {}).get("innerInstructions") or []:
        if isinstance(inner, dict):
            instructions.extend(inner.get("instructions") or [])
    for instruction in instructions:
        source = _transfer_source(instruction, wanted)
        if source:
            return source
    return None


def early_slot_bundle_size(events) -> tuple[int | None, str | None]:
    """Largest set of early buyers that share a slot or a funding wallet.

    events are {slot, buyers, funder?, missing?, error?}. An unreadable
    transaction or a window with no buy is not a measurement.
    """
    if not events:
        return None, "no signatures"
    by_slot: dict = {}
    by_funder: dict = {}
    saw_buyer = False
    for event in events:
        if not isinstance(event, dict) or event.get("missing"):
            return None, (event.get("error") if isinstance(event, dict) else None) or "tx missing"
        buyers = event.get("buyers")
        if not isinstance(buyers, (list, set, tuple)):
            return None, "tx missing"
        slot = event.get("slot")
        group = by_slot.setdefault(slot, set())
        funder = event.get("funder")
        funded = by_funder.setdefault(funder, set()) if isinstance(funder, str) and funder else None
        for buyer in buyers:
            if not isinstance(buyer, str) or not buyer:
                continue
            group.add(buyer)
            if funded is not None and buyer != funder:
                funded.add(buyer)
            saw_buyer = True
    if not saw_buyer:
        return None, "early buys missing"
    clusters = [len(group) for group in by_slot.values()]
    clusters.extend(len(group) for group in by_funder.values() if group)
    return max(clusters), None


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
    """Token accounts from getProgramAccounts or a result.value list.

    getProgramAccounts returns the rows on result itself. getTokenAccountsByOwner
    nests them under result.value. A present empty list is a real empty read.
    """
    if not isinstance(response, dict) or "result" not in response:
        return None
    result = response.get("result")
    if isinstance(result, list):
        value = result
    elif isinstance(result, dict) and isinstance(result.get("value"), list):
        value = result["value"]
    else:
        return None
    return [token_account_fields(item) for item in value if isinstance(item, dict)]
