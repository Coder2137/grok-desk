"""Auditor. Eight layers, stop at the first red. Seven of eight is a no."""

import asyncio

import httpx

from config import iso, parse_iso, utc_now
from fields import (
    MISSING,
    graph_nodes_links,
    program_account_rows,
    rpc_mint_account,
    rpc_supply_ui,
    rug_lp,
    token_account_fields,
)
from logutil import file_logger, log
from sources import TOKEN_2022, TOKEN_PROGRAM, RpcRefused, rpc, rug_graph, rug_report

LOG = file_logger("auditor", "auditor.log")

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10 holders",
    5: "Liquidity",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
}
NO_DATA = {
    1: "mint",
    2: "freeze",
    3: "dev",
    4: "top 10",
    5: "liquidity",
    6: "pool age",
    7: "bundles",
    8: "delegations",
}


def _pct(part, whole) -> float | None:
    if part is None or not whole:
        return None
    return float(part) / float(whole) * 100.0


def _fmt_pct(value: float) -> str:
    if abs(value - round(value)) < 0.05 and abs(value) >= 10:
        return str(int(round(value)))
    return f"{value:.1f}"


def biggest_bundle(nodes, links) -> int:
    holders = {}
    for node in nodes or []:
        if not isinstance(node, dict) or "id" not in node:
            continue
        holdings = node.get("holdings")
        try:
            amount = float(holdings)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            holders[node["id"]] = amount
    parent = {item: item for item in holders}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for edge in links or []:
        if not isinstance(edge, dict):
            continue
        source = edge.get("source")
        target = edge.get("target")
        if source not in parent or target not in parent:
            continue
        left, right = find(source), find(target)
        if left != right:
            parent[right] = left
    if not holders:
        return 0
    sizes: dict = {}
    for item in holders:
        root = find(item)
        sizes[root] = sizes.get(root, 0) + 1
    return max(sizes.values())


def short_addr(value: str) -> str:
    if not value or len(value) <= 8:
        return value or ""
    return f"{value[:4]}...{value[-3:]}"


def _layer(number, passed, value, source):
    return {
        "n": number,
        "name": LAYER_NAMES[number],
        "pass": passed,
        "value": value,
        "source": source,
    }


def _rest(start, layers):
    for number in range(start, 9):
        layers.append(_layer(number, None, "not checked", None))


def audit_from_facts(facts: dict) -> dict:
    """Pure 8-layer check. facts supplies already-mapped values."""
    settings = facts["settings"]
    layers = []
    holders = None

    def finish(passed, reason, checked):
        return {
            "agent": "auditor",
            "mint": facts.get("mint"),
            "ts": facts.get("ts"),
            "passed": passed,
            "layersChecked": checked,
            "layers": layers,
            "holders": holders,
            "reason": reason,
        }

    # 1 mint authority
    mint_auth = facts.get("mint_authority", MISSING)
    if mint_auth is MISSING:
        layers.append(_layer(1, False, f"no data: {NO_DATA[1]}", "rpc"))
        _rest(2, layers)
        return finish(False, layers[0]["value"], 1)
    if mint_auth is None:
        layers.append(_layer(1, True, "Revoked", "rpc"))
    else:
        layers.append(_layer(1, False, "mint not revoked", "rpc"))
        _rest(2, layers)
        return finish(False, "mint not revoked", 1)

    # 2 freeze
    freeze = facts.get("freeze_authority", MISSING)
    if freeze is MISSING:
        value = f"no data: {NO_DATA[2]}"
        layers.append(_layer(2, False, value, "rpc"))
        _rest(3, layers)
        return finish(False, value, 2)
    if freeze is None:
        layers.append(_layer(2, True, "Revoked", "rpc"))
    else:
        layers.append(_layer(2, False, "freeze not revoked", "rpc"))
        _rest(3, layers)
        return finish(False, "freeze not revoked", 2)

    # 3 dev
    supply = facts.get("supply_ui")
    if supply is None or supply <= 0:
        layers.append(_layer(3, False, "no supply", "rpc"))
        _rest(4, layers)
        return finish(False, "no supply", 3)
    creator = facts.get("creator")
    balance = facts.get("creator_balance")
    migration_balance = facts.get("creator_balance_at_migration")
    if not creator or balance is None or migration_balance is None:
        layers.append(_layer(3, False, "no data: dev", "rpc"))
        _rest(4, layers)
        return finish(False, "no data: dev", 3)
    dev_pct = _pct(balance, supply)
    if balance + 1e-9 < float(migration_balance):
        layers.append(_layer(3, False, "dev sold", "rpc"))
        _rest(4, layers)
        return finish(False, "dev sold", 3)
    if dev_pct is None or dev_pct >= float(settings["auditor.max_dev_pct"]):
        shown = _fmt_pct(dev_pct or 0)
        value = f"dev holds {shown}%"
        layers.append(_layer(3, False, value, "rpc"))
        _rest(4, layers)
        return finish(False, value, 3)
    layers.append(_layer(3, True, f"{_fmt_pct(dev_pct)}% · sold 0", "rpc"))

    # 4 top 10
    largest = facts.get("largest")
    if largest is None:
        layers.append(_layer(4, False, "no data: top 10", "rpc"))
        _rest(5, layers)
        return finish(False, "no data: top 10", 4)
    pool = facts.get("pool")
    curve = facts.get("curve")
    kept = []
    for row in largest:
        owner = row.get("owner")
        address = row.get("address")
        if pool and (owner == pool or address == pool):
            continue
        if curve and (owner == curve or address == curve):
            continue
        if row.get("balance") is None:
            continue
        kept.append(row)
    kept.sort(key=lambda item: item["balance"], reverse=True)
    top = kept[:10]
    top_pct = sum(item["balance"] for item in top) / supply * 100.0
    if top_pct >= float(settings["auditor.max_top10_pct"]):
        value = f"top 10 hold {_fmt_pct(top_pct)}%"
        layers.append(_layer(4, False, value, "rpc"))
        _rest(5, layers)
        return finish(False, value, 4)
    layers.append(_layer(4, True, f"{_fmt_pct(top_pct)}%", "rpc"))

    # 5 liquidity
    if not facts.get("lp_found"):
        layers.append(_layer(5, False, "no data: liquidity", "rugcheck"))
        _rest(6, layers)
        return finish(False, "no data: liquidity", 5)
    current_liq = facts.get("current_liquidity")
    scanner_liq = facts.get("scanner_liquidity")
    if current_liq is None or scanner_liq is None:
        layers.append(_layer(5, False, "no data: liquidity", "rugcheck"))
        _rest(6, layers)
        return finish(False, "no data: liquidity", 5)
    if float(current_liq) < float(scanner_liq) * 0.5:
        layers.append(_layer(5, False, "liquidity pulled", "rugcheck"))
        _rest(6, layers)
        return finish(False, "liquidity pulled", 5)
    locked = facts.get("lp_locked")
    burned = facts.get("lp_burned")
    need = float(settings["auditor.min_lp_locked_pct"])
    if burned is not None and float(burned) >= need:
        shown = int(round(float(burned)))
        layers.append(_layer(5, True, f"{shown}% burned", "rugcheck"))
    elif locked is not None and float(locked) >= need:
        shown = int(round(float(locked)))
        layers.append(_layer(5, True, f"{shown}% locked", "rugcheck"))
    elif locked is None and burned is None:
        layers.append(_layer(5, False, "no data: liquidity", "rugcheck"))
        _rest(6, layers)
        return finish(False, "no data: liquidity", 5)
    else:
        layers.append(_layer(5, False, "liquidity unlocked", "rugcheck"))
        _rest(6, layers)
        return finish(False, "liquidity unlocked", 5)

    # 6 age
    migrated = facts.get("migrated_at")
    if not migrated:
        layers.append(_layer(6, False, "no data: pool age", "rpc"))
        _rest(7, layers)
        return finish(False, "no data: pool age", 6)
    moment = parse_iso(migrated) if isinstance(migrated, str) else migrated
    now = facts.get("now") or utc_now()
    age_min = (now - moment).total_seconds() / 60.0
    if age_min >= float(settings["auditor.min_pool_age_min"]):
        layers.append(_layer(6, True, f"{int(age_min)}m", "rpc"))
    else:
        layers.append(_layer(6, False, "pool too young", "rpc"))
        _rest(7, layers)
        return finish(False, "pool too young", 6)

    # 7 bundles
    if facts.get("links_missing") or facts.get("empty_network"):
        layers.append(_layer(7, False, "no data: bundles", "rugcheck"))
        _rest(8, layers)
        return finish(False, "no data: bundles", 7)
    size = biggest_bundle(facts.get("nodes") or [], facts.get("links") or [])
    if size >= int(settings["auditor.max_bundled"]):
        value = f"{size} bundled wallets"
        layers.append(_layer(7, False, value, "rugcheck"))
        _rest(8, layers)
        return finish(False, value, 7)
    layers.append(_layer(7, True, "None" if size <= 1 else str(size), "rugcheck"))

    # 8 delegations
    if facts.get("delegations_down"):
        layers.append(_layer(8, False, "no data: delegations", "rpc"))
        return finish(False, "no data: delegations", 8)
    accounts = facts.get("delegation_accounts")
    if accounts is None:
        layers.append(_layer(8, False, "no data: delegations", "rpc"))
        return finish(False, "no data: delegations", 8)
    holders = sum(1 for row in accounts if (row.get("balance") or 0) > 0)
    counts: dict[str, int] = {}
    for row in accounts:
        if (row.get("balance") or 0) <= 0:
            continue
        delegate = row.get("delegate")
        if not delegate:
            continue
        counts[delegate] = counts.get(delegate, 0) + 1
    if counts:
        delegate, count = max(counts.items(), key=lambda item: item[1])
        if count > int(settings["auditor.max_delegated_accounts"]):
            layers.append(_layer(8, False, f"{count} wallets → {short_addr(delegate)}", "rpc"))
            return finish(False, layers[-1]["value"], 8)
    layers.append(_layer(8, True, "None", "rpc"))
    return finish(True, None, 8)


class Auditor:
    def __init__(self, db, settings, bus) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self._announced_layer8 = False

    async def run(self) -> None:
        while True:
            message = await self.bus.auditor.get()
            try:
                if self.settings.get("auditor.enabled") and not self.settings.get("agent.auditor.paused"):
                    await self.handle(message)
            except Exception as exc:
                log(LOG, f"auditor error {exc.__class__.__name__}")
            self.db.heartbeat("auditor")

    async def handle(self, message: dict, enqueue: bool = True) -> dict:
        mint = message["mint"]
        since = self.settings.get("desk.night_start") or "1970"
        if self.db.rejected_tonight(mint, since):
            return {"passed": False, "reason": "already rejected", "layers": []}
        facts = await self.gather(message)
        result = audit_from_facts(facts)
        self.db.clear_audit(mint)
        for layer in result["layers"]:
            self.db.add_audit_layer(
                mint,
                result["ts"],
                layer["n"],
                layer["name"],
                layer["value"],
                layer["pass"],
                layer["source"],
            )
        if result.get("holders") is not None:
            self.db.upsert_coin(mint=mint, holders=result["holders"])
        parts = " ".join(f"L{layer['n']}={layer['value']}" for layer in result["layers"])
        log(LOG, f"{mint} {parts} verdict {'pass' if result['passed'] else result['reason']}")
        if result["passed"] and enqueue:
            onward = dict(message)
            onward["audit"] = result
            onward["bundled"] = _bundle_count(result)
            onward["holders"] = result["holders"]
            onward["devPct"] = _dev_number(facts)
            onward["lpText"] = next(layer["value"] for layer in result["layers"] if layer["n"] == 5)
            onward["delegationsText"] = next(layer["value"] for layer in result["layers"] if layer["n"] == 8)
            onward["top10Pct"] = message.get("top10Pct")
            await self.bus.analyst.put(onward)
        elif not result["passed"]:
            name = message.get("name") or mint
            self.db.add_activity(
                "rejected",
                mint,
                "Auditor",
                f"Rejected {name}",
                f"Auditor · {result['reason']}",
                "Skipped",
            )
        return result

    async def gather(self, message: dict) -> dict:
        settings = self.settings.all()
        mint = message["mint"]
        coin = self.db.coin(mint)
        retries = int(settings["auditor.retries"])
        async with httpx.AsyncClient() as client:
            report = await self._retry(lambda: rug_report(mint, client), retries)
            graph = await self._retry(lambda: rug_graph(mint, client), retries)
            rpc_url = settings["desk.rpc_url"]
            mint_resp = await self._retry(
                lambda: rpc(rpc_url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}], client),
                retries,
            )
            creator = None
            if coin and coin["creator"]:
                creator = coin["creator"]
            elif isinstance(report, dict):
                from fields import rug_report_fields

                creator = rug_report_fields(report).get("creator")
            creator_balance = None
            if creator:
                owned = await self._retry(
                    lambda: rpc(
                        rpc_url,
                        "getTokenAccountsByOwner",
                        [creator, {"mint": mint}, {"encoding": "jsonParsed"}],
                        client,
                    ),
                    retries,
                )
                creator_balance = _sum_owner_balance(owned)
            largest = await self._largest(rpc_url, mint, client, retries)
            accounts = None
            delegations_down = bool(self.settings.get("desk.layer8_down"))
            if not delegations_down:
                try:
                    accounts = await self._delegations(rpc_url, mint, client, retries)
                except RpcRefused:
                    delegations_down = True
                    self.settings.set("desk.layer8_down", True, "auditor")
                    if not self._announced_layer8:
                        self._announced_layer8 = True
                        await self.bus.events.put(
                            {
                                "type": "text",
                                "text": "layer 8 down, every coin is red until rpc_url changes",
                            }
                        )
        mint_account = rpc_mint_account(mint_resp) if mint_resp else {
            "mintAuthority": MISSING,
            "freezeAuthority": MISSING,
            "supply": None,
            "decimals": None,
        }
        nodes, links, links_missing, empty_network = graph_nodes_links(graph)
        lp = rug_lp(report or {}, message.get("pool") or (coin["pool"] if coin else None))
        migrated = coin["migrated_at"] if coin else message.get("migratedAt")
        migration_balance = coin["creator_balance_at_migration"] if coin else None
        return {
            "mint": mint,
            "ts": iso(utc_now()),
            "settings": settings,
            "pool": message.get("pool") or (coin["pool"] if coin else None),
            "curve": message.get("curve"),
            "migrated_at": migrated,
            "now": utc_now(),
            "creator": creator,
            "creator_balance": creator_balance,
            "creator_balance_at_migration": migration_balance,
            "supply_ui": rpc_supply_ui(mint_account),
            "mint_authority": mint_account["mintAuthority"],
            "freeze_authority": mint_account["freezeAuthority"],
            "largest": largest,
            "lp_found": lp["found"],
            "lp_locked": lp["locked"],
            "lp_burned": lp["burned"],
            "current_liquidity": (message.get("dex") or {}).get("liquidityUsd", message.get("liquidityUsd")),
            "scanner_liquidity": message.get("liquidityUsd"),
            "nodes": nodes,
            "links": links,
            "links_missing": links_missing,
            "empty_network": empty_network,
            "delegations_down": delegations_down,
            "delegation_accounts": accounts,
        }

    async def _retry(self, fn, retries: int):
        last = None
        for attempt in range(retries):
            try:
                value = await fn()
                if value is not None:
                    return value
            except RpcRefused:
                raise
            except Exception as exc:
                last = exc
            if attempt + 1 < retries:
                await asyncio.sleep(2)
        if last:
            log(LOG, f"source failed {last.__class__.__name__}")
        return None

    async def _largest(self, rpc_url, mint, client, retries):
        body = await self._retry(
            lambda: rpc(rpc_url, "getTokenLargestAccounts", [mint], client),
            retries,
        )
        if not body:
            return None
        from fields import largest_account_rows

        rows = largest_account_rows(body)
        if rows is None:
            return None
        addresses = [row["address"] for row in rows]
        if not addresses:
            return []
        parsed = await self._retry(
            lambda: rpc(rpc_url, "getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}], client),
            retries,
        )
        value = None
        if isinstance(parsed, dict):
            value = parsed.get("result", {}).get("value") if isinstance(parsed.get("result"), dict) else None
        if not isinstance(value, list):
            return None
        out = []
        for address, account in zip(addresses, value):
            wrapped = {"account": account} if isinstance(account, dict) else {}
            fields = token_account_fields(wrapped)
            fields["address"] = address
            out.append(fields)
        return out

    async def _delegations(self, rpc_url, mint, client, retries):
        rows = []
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            body = await self._retry(
                lambda program=program: rpc(
                    rpc_url,
                    "getProgramAccounts",
                    [
                        program,
                        {
                            "encoding": "jsonParsed",
                            "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                        },
                    ],
                    client,
                ),
                retries,
            )
            if body is None:
                return None
            parsed = program_account_rows(body)
            if parsed is None:
                return None
            rows.extend(parsed)
        return rows


def _sum_owner_balance(response) -> float | None:
    if not isinstance(response, dict):
        return None
    value = response.get("result", {}).get("value") if isinstance(response.get("result"), dict) else None
    if not isinstance(value, list):
        return None
    total = 0.0
    for item in value:
        fields = token_account_fields(item if isinstance(item, dict) else {})
        if fields["balance"] is not None:
            total += fields["balance"]
    return total


def _bundle_count(result) -> int:
    for layer in result["layers"]:
        if layer["n"] == 7 and layer["pass"]:
            if layer["value"] == "None":
                return 0
            try:
                return int(layer["value"])
            except (TypeError, ValueError):
                return 0
        if layer["n"] == 7 and layer["pass"] is False and "bundled" in str(layer["value"]):
            try:
                return int(str(layer["value"]).split()[0])
            except (TypeError, ValueError):
                return 0
    return 0


def _dev_number(facts) -> float | None:
    supply = facts.get("supply_ui")
    balance = facts.get("creator_balance")
    if not supply or balance is None:
        return None
    return balance / supply * 100.0
