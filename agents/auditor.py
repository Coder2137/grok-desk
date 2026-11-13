"""Auditor. Eight layers, stop at the first red. Seven of eight is a no."""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx

import auditcache
from config import iso, parse_iso, utc_now
from fields import (
    MISSING,
    buyers_from_transaction,
    early_slot_bundle_size,
    funder_from_transaction,
    graph_nodes_links,
    insider_bundle_size,
    permanent_delegate_from_mint,
    rpc_mint_account,
    rpc_supply_ui,
    rug_lp,
    token_account_fields,
)
from logutil import file_logger, log
from sources import TOKEN_2022, TOKEN_PROGRAM, RpcRefused, dexscreener_pair, rpc, rpc_urls, rug_graph, rug_report

LOG = file_logger("auditor", "auditor.log")
HEARTBEAT_S = 30
# Three readings. A transient miss waits 2 minutes, then 5. The next try is
# dropped when it would land after the scanner's pool-age window.
MAX_AUDIT_TRIES = 3
RETRY_DELAYS_S = (120, 300)
_RPC_GAP_S = 0.2
_RETRY_KEYS = (
    "mint",
    "name",
    "symbol",
    "pool",
    "curve",
    "liquidityUsd",
    "top10Pct",
    "migratedAt",
    "dex",
    "audit_attempt",
)

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


def _solana_address(value) -> str | None:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not isinstance(value, str) or not (32 <= len(value) <= 44):
        return None
    if any(ch not in alphabet for ch in value):
        return None
    return value


def short_addr(value: str) -> str:
    if not value or len(value) <= 8:
        return value or ""
    return f"{value[:4]}...{value[-3:]}"


def is_transient_cause(cause: str | None) -> bool:
    """A rate limit, a timeout, or an unfinished early window. Not a measurement."""
    if not cause:
        return False
    text = str(cause).lower()
    if "429" in text or "timeout" in text or "early window incomplete" in text:
        return True
    return False


def is_transient_reason(reason: str | None) -> bool:
    if not reason or not str(reason).startswith("no data:"):
        return False
    return is_transient_cause(reason)


def retry_file() -> Path:
    override = os.environ.get("DESK_AUDIT_RETRY")
    if override:
        return Path(override)
    db = os.environ.get("DESK_DB") or "var/desk.db"
    return Path(db).with_name(Path(db).name + ".audit-retry.json")


def _read_retries() -> dict:
    try:
        data = json.loads(retry_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    mints = data.get("mints") if isinstance(data, dict) else None
    return mints if isinstance(mints, dict) else {}


def _write_retries(mints: dict) -> None:
    path = retry_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"v": 1, "mints": mints}, separators=(",", ":")), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return


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
        cause = facts.get("dev_error")
        if not cause:
            if not creator:
                cause = "creator absent"
            elif migration_balance is None:
                cause = "migration balance missing"
            else:
                cause = "balance missing"
        value = f"no data: dev ({cause})"
        layers.append(_layer(3, False, value, "rpc"))
        _rest(4, layers)
        return finish(False, value, 3)
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
    measured = facts.get("top10_pct")
    largest = facts.get("largest")
    if measured is None and largest is None:
        cause = facts.get("top10_error") or "missing"
        value = f"no data: top 10 ({cause})"
        layers.append(_layer(4, False, value, "rpc"))
        _rest(5, layers)
        return finish(False, value, 4)
    if measured is None:
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
    else:
        top_pct = float(measured)
    if top_pct >= float(settings["auditor.max_top10_pct"]):
        value = f"top 10 hold {_fmt_pct(top_pct)}%"
        layers.append(_layer(4, False, value, "rpc"))
        _rest(5, layers)
        return finish(False, value, 4)
    layers.append(_layer(4, True, f"{_fmt_pct(top_pct)}%", "rpc"))

    # 5 liquidity
    if not facts.get("lp_found"):
        cause = facts.get("liquidity_error") or "missing"
        value = f"no data: liquidity ({cause})"
        layers.append(_layer(5, False, value, "rugcheck"))
        _rest(6, layers)
        return finish(False, value, 5)
    current_liq = facts.get("current_liquidity")
    scanner_liq = facts.get("scanner_liquidity")
    if current_liq is None or scanner_liq is None:
        value = "no data: liquidity (amount missing)"
        layers.append(_layer(5, False, value, "rugcheck"))
        _rest(6, layers)
        return finish(False, value, 5)
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
        value = "no data: liquidity (lock missing)"
        layers.append(_layer(5, False, value, "rugcheck"))
        _rest(6, layers)
        return finish(False, value, 5)
    else:
        layers.append(_layer(5, False, "liquidity unlocked", "rugcheck"))
        _rest(6, layers)
        return finish(False, "liquidity unlocked", 5)

    # 6 age
    migrated = facts.get("migrated_at")
    if not migrated:
        value = "no data: pool age (migrated_at missing)"
        layers.append(_layer(6, False, value, "rpc"))
        _rest(7, layers)
        return finish(False, value, 6)
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
    if "bundle_size" in facts:
        size = facts.get("bundle_size")
        if isinstance(size, bool) or not isinstance(size, (int, float)):
            cause = facts.get("bundle_error") or "missing"
            value = f"no data: bundles ({cause})"
            layers.append(_layer(7, False, value, "rugcheck"))
            _rest(8, layers)
            return finish(False, value, 7)
        size = int(size)
    elif facts.get("links_missing") or facts.get("empty_network"):
        if facts.get("bundle_error"):
            cause = facts["bundle_error"]
        elif facts.get("links_missing"):
            cause = "links missing"
        else:
            cause = "empty network"
        value = f"no data: bundles ({cause})"
        layers.append(_layer(7, False, value, "rugcheck"))
        _rest(8, layers)
        return finish(False, value, 7)
    else:
        size = biggest_bundle(facts.get("nodes") or [], facts.get("links") or [])
    if size >= int(settings["auditor.max_bundled"]):
        value = f"{size} bundled wallets"
        layers.append(_layer(7, False, value, "rugcheck"))
        _rest(8, layers)
        return finish(False, value, 7)
    layers.append(_layer(7, True, "None" if size <= 1 else str(size), "rugcheck"))

    # 8 delegations
    accounts = facts.get("delegation_accounts")
    if facts.get("delegations_down") or accounts is None or accounts == []:
        cause = facts.get("delegations_error") or "missing"
        value = f"no data: delegations ({cause})"
        layers.append(_layer(8, False, value, "rpc"))
        return finish(False, value, 8)
    if any(row.get("read") is False for row in accounts):
        value = "no data: delegations (account not found)"
        layers.append(_layer(8, False, value, "rpc"))
        return finish(False, value, 8)
    permanent = facts.get("permanent_delegate")
    if not permanent:
        permanent = next((row.get("permanent_delegate") for row in accounts if row.get("permanent_delegate")), None)
    if permanent:
        value = f"permanent delegate {short_addr(permanent)}"
        layers.append(_layer(8, False, value, "rpc"))
        return finish(False, value, 8)
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
        self._pending: dict[str, tuple[str, dict]] = {}
        self._rpc_turn = 0
        self._rpc_lock = None
        self._sleep = asyncio.sleep

    def _lock(self) -> asyncio.Lock:
        if self._rpc_lock is None:
            self._rpc_lock = asyncio.Lock()
        return self._rpc_lock

    async def run(self) -> None:
        while True:
            self._load_pending()
            self._release_due()
            try:
                message = await asyncio.wait_for(self.bus.auditor.get(), timeout=self._next_timeout())
            except asyncio.TimeoutError:
                self.db.heartbeat("auditor")
                continue
            try:
                if self.settings.get("auditor.enabled") and not self.settings.get("agent.auditor.paused"):
                    await self.handle(message)
            except Exception as exc:
                log(LOG, f"auditor error {exc.__class__.__name__}")
            self.db.heartbeat("auditor")

    def _next_timeout(self) -> float:
        wait = float(HEARTBEAT_S)
        now = utc_now()
        for due, _message in self._pending.values():
            try:
                remaining = (parse_iso(due) - now).total_seconds()
            except (TypeError, ValueError):
                remaining = 0
            wait = min(wait, max(0.0, remaining))
        return wait

    def _release_due(self) -> None:
        now = utc_now()
        ready = []
        for mint, (due, _message) in self._pending.items():
            try:
                moment = parse_iso(due)
            except (TypeError, ValueError):
                ready.append(mint)
                continue
            if moment <= now:
                ready.append(mint)
        for mint in ready:
            _due, message = self._pending.pop(mint)
            self.bus.auditor.put_nowait(message)
        if ready:
            self._store_pending()

    def _load_pending(self) -> None:
        for mint, row in _read_retries().items():
            if mint in self._pending or not isinstance(row, dict):
                continue
            message = row.get("message")
            due = row.get("due")
            if isinstance(message, dict) and isinstance(due, str) and message.get("mint"):
                self._pending[str(mint)] = (due, message)

    def _store_pending(self) -> None:
        mints = {}
        for mint, (due, message) in self._pending.items():
            mints[mint] = {"due": due, "message": message}
        _write_retries(mints)

    def _clear_retry(self, mint: str) -> None:
        self._pending.pop(mint, None)
        if not retry_file().is_file():
            return
        mints = _read_retries()
        if mint not in mints:
            return
        mints.pop(mint, None)
        _write_retries(mints)

    def _defer(self, message: dict, result: dict) -> bool:
        """Keep a transient miss open. False means this reading is the last one."""
        try:
            attempt = int(message.get("audit_attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt < 0 or attempt + 1 >= MAX_AUDIT_TRIES:
            return False
        delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
        if not self._inside_pool_window(message, delay):
            return False
        nxt = {key: message.get(key) for key in _RETRY_KEYS if key in message}
        nxt["mint"] = message["mint"]
        nxt["audit_attempt"] = attempt + 1
        due = iso(utc_now() + timedelta(seconds=delay))
        self._pending[message["mint"]] = (due, nxt)
        self._store_pending()
        result["retrying"] = True
        result["retry_in_s"] = delay
        return True

    def _inside_pool_window(self, message: dict, delay_s: int) -> bool:
        try:
            limit = float(self.settings.get("scanner.max_wait_min"))
        except (TypeError, ValueError):
            limit = 60.0
        coin = self.db.coin(message["mint"])
        migrated = None
        if coin and coin["migrated_at"]:
            migrated = coin["migrated_at"]
        elif message.get("migratedAt"):
            migrated = message["migratedAt"]
        if not migrated:
            return True
        moment = parse_iso(migrated) if isinstance(migrated, str) else migrated
        age_min = (utc_now() + timedelta(seconds=delay_s) - moment).total_seconds() / 60.0
        return age_min <= limit

    def _write_layers(self, mint: str, result: dict, retrying: bool) -> None:
        self.db.clear_audit(mint)
        failed = result.get("reason")
        for layer in result["layers"]:
            value = layer["value"]
            passed = layer["pass"]
            if retrying and value == failed:
                value = "retrying"
                passed = None
                layer["value"] = value
                layer["pass"] = None
            self.db.add_audit_layer(
                mint,
                result["ts"],
                layer["n"],
                layer["name"],
                value,
                passed,
                layer["source"],
            )

    async def handle(self, message: dict, enqueue: bool = True) -> dict:
        mint = message["mint"]
        since = self.settings.get("desk.night_start") or "1970"
        if self.db.rejected_tonight(mint, since):
            return {"passed": False, "reason": "already rejected", "layers": []}
        facts = await self.gather(message)
        result = audit_from_facts(facts)
        retrying = not result["passed"] and is_transient_reason(result.get("reason")) and self._defer(message, result)
        if result["passed"] or not retrying:
            self._clear_retry(mint)
        self._write_layers(mint, result, retrying)
        if result.get("holders") is not None:
            self.db.upsert_coin(mint=mint, holders=result["holders"])
        parts = " ".join(f"L{layer['n']}={layer['value']}" for layer in result["layers"])
        if retrying:
            log(LOG, f"{mint} {parts} retrying in {result.get('retry_in_s')}s")
            name = message.get("name") or mint
            self.db.add_activity(
                "retrying",
                mint,
                "Auditor",
                f"Retrying {name}",
                "Auditor · retrying",
                "Later",
            )
            return result
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
            from fields import rug_report_fields

            rug_fields = rug_report_fields(report) if isinstance(report, dict) else {}
            creator = None
            if coin and coin["creator"]:
                creator = coin["creator"]
            else:
                creator = rug_fields.get("creator")
            migration_balance = coin["creator_balance_at_migration"] if coin else None
            creator_balance = None
            dev_error = None
            if not creator:
                dev_error = "creator absent"
            elif migration_balance is None:
                dev_error = "migration balance missing"
            else:
                creator_balance, dev_error = await self._creator_balance(
                    client, rpc_url, creator, mint, rug_fields, retries
                )
            pool = message.get("pool") or (coin["pool"] if coin else None)
            curve = message.get("curve")
            top10_value, top10_error = self._top10_from_report(rug_fields, message, pool, curve)
            largest = None
            if top10_value is None:
                largest, rpc_error = await self._largest(rpc_url, mint, client, retries)
                if largest is None:
                    top10_error = rpc_error or top10_error
                else:
                    top10_error = None
            cached_delegations = auditcache.load_layer(mint, 8)
            if isinstance(cached_delegations, dict) and isinstance(cached_delegations.get("accounts"), list):
                accounts, delegations_error = cached_delegations["accounts"], None
            elif (
                isinstance(cached_delegations, dict)
                and cached_delegations.get("error")
                and not is_transient_cause(cached_delegations.get("error"))
            ):
                accounts, delegations_error = None, cached_delegations["error"]
            else:
                accounts, delegations_error = await self._delegations(rpc_url, mint, client, retries)
                if accounts is not None or (delegations_error and not is_transient_cause(delegations_error)):
                    auditcache.store_layer(mint, 8, {"accounts": accounts, "error": delegations_error})
            delegations_down = False
            cached_bundles = auditcache.load_layer(mint, 7)
            if isinstance(cached_bundles, dict) and isinstance(cached_bundles.get("bundle_size"), int) and not isinstance(
                cached_bundles.get("bundle_size"), bool
            ):
                bundle_size, bundle_error = cached_bundles["bundle_size"], None
            else:
                bundle_size, bundle_error = insider_bundle_size(graph, report if isinstance(report, dict) else None)
                if bundle_size is None:
                    measured, measured_error = await self._early_bundle(client, rpc_url, pool, mint, retries)
                    if measured is not None:
                        bundle_size, bundle_error = measured, None
                    else:
                        bundle_error = measured_error or bundle_error or "missing"
                if bundle_size is not None or (bundle_error and not is_transient_cause(bundle_error)):
                    auditcache.store_layer(mint, 7, {"bundle_size": bundle_size, "bundle_error": bundle_error})
            pool_for_lp = pool
            lp = rug_lp(report or {}, pool_for_lp)
            dex_liquidity = None
            if report is None:
                liquidity_error = "rugcheck unavailable"
            elif not lp["found"]:
                try:
                    dex_liquidity, dex_error = await dexscreener_pair(pool_for_lp or "", client)
                except Exception:
                    dex_liquidity, dex_error = None, "dexscreener unavailable"
                if dex_liquidity is not None:
                    lp = {"found": True, "locked": None, "burned": None}
                    liquidity_error = "lock missing"
                else:
                    liquidity_error = dex_error or "pool not in report"
            elif lp["locked"] is None and lp["burned"] is None:
                liquidity_error = "lock missing"
            else:
                liquidity_error = None
        mint_account = rpc_mint_account(mint_resp) if mint_resp else {
            "mintAuthority": MISSING,
            "freezeAuthority": MISSING,
            "supply": None,
            "decimals": None,
        }
        nodes, links, links_missing, empty_network = graph_nodes_links(graph)
        if bundle_size is not None:
            links_missing = False
            empty_network = False
        elif not bundle_error:
            bundle_error = "missing"
        migrated = coin["migrated_at"] if coin else message.get("migratedAt")
        return {
            "mint": mint,
            "ts": iso(utc_now()),
            "settings": settings,
            "pool": pool_for_lp,
            "curve": message.get("curve"),
            "migrated_at": migrated,
            "now": utc_now(),
            "creator": creator,
            "creator_balance": creator_balance,
            "creator_balance_at_migration": migration_balance,
            "dev_error": dev_error,
            "supply_ui": rpc_supply_ui(mint_account),
            "mint_authority": mint_account["mintAuthority"],
            "freeze_authority": mint_account["freezeAuthority"],
            "largest": largest,
            "top10_pct": top10_value,
            "top10_error": top10_error,
            "liquidity_error": liquidity_error,
            "bundle_error": bundle_error,
            "bundle_size": bundle_size,
            "delegations_error": delegations_error,
            "lp_found": lp["found"],
            "lp_locked": lp["locked"],
            "lp_burned": lp["burned"],
            "current_liquidity": dex_liquidity
            if dex_liquidity is not None
            else (message.get("dex") or {}).get("liquidityUsd", message.get("liquidityUsd")),
            "scanner_liquidity": message.get("liquidityUsd"),
            "nodes": nodes,
            "links": links,
            "links_missing": links_missing,
            "empty_network": empty_network,
            "delegations_down": delegations_down,
            "delegation_accounts": accounts,
            "permanent_delegate": permanent_delegate_from_mint(mint_resp),
        }

    async def _creator_balance(self, client, rpc_url, creator, mint, rug_fields, retries):
        """Current dev balance. RugCheck is only a fallback for this reading.

        The migration baseline stays the balance stored when the coin was
        admitted. A later RugCheck number is not copied into that baseline.
        """
        last = "rpc unavailable"
        for attempt in range(max(1, retries)):
            try:
                response = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [creator, {"mint": mint}, {"encoding": "jsonParsed"}],
                    },
                    timeout=20,
                )
            except Exception as exc:
                last = exc.__class__.__name__
            else:
                if response.status_code == 429:
                    last = "rpc 429"
                elif response.status_code >= 400:
                    last = f"rpc http {response.status_code}"
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                        last = "rpc report"
                    if isinstance(body, dict) and not body.get("error"):
                        total = _sum_owner_balance(body)
                        if total is not None:
                            return total, None
                        last = "balance missing"
                    elif isinstance(body, dict) and body.get("error"):
                        last = "rpc error"
            if attempt + 1 < retries:
                await asyncio.sleep(min(8, 2 ** attempt))
        if rug_fields.get("creator") == creator and rug_fields.get("creatorBalance") is not None:
            return rug_fields["creatorBalance"], None
        return None, last

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

    def _top10_from_report(self, rug_fields: dict, message: dict, pool, curve):
        """Same holder math as the scanner. The scan's top10Pct is the fallback."""
        from agents.scanner import top10_from_holders

        holders = rug_fields.get("topHolders") if isinstance(rug_fields, dict) else None
        if isinstance(holders, list) and holders:
            measured = top10_from_holders(holders, pool, curve)
            if measured is not None:
                return measured, None
            holders_error = "topHolders missing"
        elif rug_fields:
            holders_error = "topHolders missing"
        else:
            holders_error = "rugcheck unavailable"
        scanned = message.get("top10Pct")
        if isinstance(scanned, (int, float)) and not isinstance(scanned, bool):
            return float(scanned), None
        return None, holders_error

    async def _rpc_call(self, client, rpc_url, method, params, retries):
        last = "rpc unavailable"
        for attempt in range(max(1, retries)):
            try:
                response = await client.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=20,
                )
            except Exception as exc:
                last = exc.__class__.__name__
            else:
                if response.status_code == 429:
                    last = "rpc 429"
                elif response.status_code in (401, 403):
                    return None, "rpc refused"
                elif response.status_code >= 400:
                    last = f"rpc http {response.status_code}"
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                        last = "rpc report"
                    if isinstance(body, dict) and body.get("error"):
                        message = body["error"].get("message", "") if isinstance(body["error"], dict) else ""
                        lowered = str(message).lower()
                        if "not allowed" in lowered or "excluded" in lowered or "forbidden" in lowered:
                            return None, "rpc refused"
                        last = "rpc error"
                    elif isinstance(body, dict):
                        return body, None
            if attempt + 1 < retries:
                await asyncio.sleep(min(8, 2 ** attempt))
        return None, last

    async def _largest(self, rpc_url, mint, client, retries):
        body, error = await self._rpc_call(client, rpc_url, "getTokenLargestAccounts", [mint], retries)
        if not body:
            return None, error
        from fields import largest_account_rows

        rows = largest_account_rows(body)
        if rows is None:
            return None, "largest accounts missing"
        addresses = [row["address"] for row in rows]
        if not addresses:
            return [], None
        parsed, error = await self._rpc_call(
            client,
            rpc_url,
            "getMultipleAccounts",
            [addresses, {"encoding": "jsonParsed"}],
            retries,
        )
        value = None
        if isinstance(parsed, dict):
            result = parsed.get("result")
            value = result.get("value") if isinstance(result, dict) else None
            if result is None and "result" in parsed:
                return None, "account not found"
        if not isinstance(value, list):
            return None, error or "largest accounts missing"
        if any(item is None for item in value):
            return None, "account not found"
        out = []
        for address, account in zip(addresses, value):
            wrapped = {"account": account} if isinstance(account, dict) else {}
            fields = token_account_fields(wrapped)
            fields["address"] = address
            if not fields.get("read"):
                return None, "account not found"
            out.append(fields)
        if not out:
            return None, "account not found"
        return out, None

    async def _rpc_across(self, rpc_url, method, params, client, retries):
        errors = []
        for url in rpc_urls(rpc_url):
            body, error = await self._rpc_call(client, url, method, params, retries)
            if body is not None:
                return body, None
            if error:
                errors.append(error)
        return None, self._prefer_rpc_error(errors) if errors else "rpc unavailable"

    def _rotate(self, primary: str) -> list[str]:
        urls = rpc_urls(primary)
        if len(urls) > 1 and self._rpc_turn % 2:
            urls = list(reversed(urls))
        self._rpc_turn += 1
        return urls

    async def _rpc_once(self, client, rpc_url, method, params):
        """One RPC post. Returns body, error, and Retry-After seconds when present."""
        try:
            response = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=20,
            )
        except Exception as exc:
            name = exc.__class__.__name__
            if "timeout" in name.lower():
                return None, "timeout", None
            return None, name, None
        retry_after = None
        header = response.headers.get("Retry-After") if response.headers else None
        if header:
            try:
                retry_after = max(1, int(float(header)))
            except (TypeError, ValueError):
                retry_after = None
        if response.status_code == 429:
            return None, "rpc 429", retry_after
        if response.status_code in (401, 403):
            return None, "rpc refused", None
        if response.status_code >= 400:
            return None, f"rpc http {response.status_code}", None
        try:
            body = response.json()
        except ValueError:
            return None, "rpc report", None
        if isinstance(body, dict) and body.get("error"):
            message = body["error"].get("message", "") if isinstance(body["error"], dict) else ""
            lowered = str(message).lower()
            if "not allowed" in lowered or "excluded" in lowered or "forbidden" in lowered:
                return None, "rpc refused", None
            return None, "rpc error", None
        if isinstance(body, dict):
            return body, None, None
        return None, "rpc report", None

    async def _paced_rpc(self, client, primary, method, params, retries):
        """One early-buyer call at a time. Rotate the two public RPCs and back off."""
        async with self._lock():
            urls = self._rotate(primary)
            last = "rpc unavailable"
            rounds = max(1, int(retries))
            for round_index in range(rounds):
                wait = None
                refused = None
                for url in urls:
                    await self._sleep(_RPC_GAP_S)
                    body, error, retry_after = await self._rpc_once(client, url, method, params)
                    if body is not None:
                        return body, None
                    if error == "rpc refused":
                        refused = error
                        continue
                    last = error or last
                    if retry_after:
                        wait = retry_after if wait is None else max(wait, retry_after)
                if refused and last == "rpc unavailable":
                    return None, refused
                if round_index + 1 >= rounds:
                    break
                delay = wait if wait is not None else min(8, 2 ** round_index)
                await self._sleep(max(1, delay))
            return None, last

    def _remember_pages(self, address: str, collected: list, before: str | None, done: bool) -> None:
        if not collected and not done:
            return
        auditcache.store_pages(address, collected, before, done)

    async def _signature_pages(self, client, rpc_url, address, retries):
        saved = auditcache.load_pages(address)
        collected = []
        before = None
        seen = set()
        if saved:
            collected = [row for row in saved.get("signatures") or [] if isinstance(row, dict)]
            if saved.get("done"):
                return collected, None, False
            before = saved.get("before") if isinstance(saved.get("before"), str) else None
            if before:
                seen.add(before)
        while True:
            options = {"limit": 1000}
            if before:
                options["before"] = before
            body, error = await self._paced_rpc(
                client, rpc_url, "getSignaturesForAddress", [address, options], retries
            )
            if body is None:
                self._remember_pages(address, collected, before, False)
                return None, error or "rpc unavailable", False
            result = body.get("result")
            if not isinstance(result, list):
                self._remember_pages(address, collected, before, False)
                return None, "signatures missing", False
            collected.extend(item for item in result if isinstance(item, dict))
            if len(result) < 1000:
                self._remember_pages(address, collected, before, True)
                return collected, None, False
            last = result[-1].get("signature") if isinstance(result[-1], dict) else None
            if not last or last in seen:
                self._remember_pages(address, collected, before, False)
                return collected, None, True
            seen.add(last)
            before = last
            self._remember_pages(address, collected, before, False)

    async def _early_bundle(self, client, rpc_url, pool, mint, retries):
        """Same-slot buyers, and buyers funded inside the same transaction.

        Used only when RugCheck has not built an insider network. A partial
        or unread window is not a measurement. One desk holds the pool while
        it pages, and a later try continues from the saved pages.
        """
        address = _solana_address(pool) or _solana_address(mint)
        if not address:
            return None, "pool missing"
        held = False
        waited = 0.0
        while not held and waited < 20:
            held = auditcache.acquire(address)
            if held:
                break
            await self._sleep(0.5)
            waited += 0.5
            cached = auditcache.load_layer(mint, 7)
            if isinstance(cached, dict) and isinstance(cached.get("bundle_size"), int) and not isinstance(
                cached.get("bundle_size"), bool
            ):
                return cached["bundle_size"], None
        try:
            return await self._read_early(client, rpc_url, address, mint, retries)
        finally:
            if held:
                auditcache.release(address)

    async def _read_early(self, client, rpc_url, address, mint, retries):
        signatures, error, truncated = await self._signature_pages(client, rpc_url, address, retries)
        if signatures is None:
            return None, error or "rpc unavailable"
        if not signatures:
            return None, "no signatures"
        if truncated:
            return None, "early window incomplete"
        usable = [
            row for row in signatures
            if isinstance(row, dict) and row.get("signature") and not row.get("err")
        ]
        if not usable:
            return None, "early buys missing"
        usable.sort(key=lambda row: (row.get("slot") or 0, row.get("transactionIndex") or 0))
        events = []
        for row in usable[:40]:
            body, tx_error = await self._paced_rpc(
                client,
                rpc_url,
                "getTransaction",
                [row["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
                retries,
            )
            result = body.get("result") if isinstance(body, dict) else None
            buyers = buyers_from_transaction(result, mint, address) if isinstance(result, dict) else None
            if buyers is None:
                return None, tx_error or "tx missing"
            events.append(
                {
                    "slot": row.get("slot"),
                    "buyers": buyers,
                    "funder": funder_from_transaction(result, buyers),
                }
            )
        return early_slot_bundle_size(events)

    def _prefer_rpc_error(self, errors: list[str]) -> str:
        for error in errors:
            if error == "rpc 429":
                return error
        for error in errors:
            if error and error != "account not found":
                return error
        return "account not found"

    async def _program_holders(self, rpc_url, mint, client, retries):
        rows = []
        errors = []
        answered = 0
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            body, error = await self._rpc_call(
                client,
                rpc_url,
                "getProgramAccounts",
                [
                    program,
                    {
                        "encoding": "jsonParsed",
                        "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                    },
                ],
                retries,
            )
            if body is None:
                errors.append(error or "rpc unavailable")
                continue
            result = body.get("result")
            if isinstance(result, dict) and isinstance(result.get("value"), list):
                items = result["value"]
            elif isinstance(result, list):
                items = result
            else:
                errors.append("accounts missing")
                continue
            if any(item is None or not isinstance(item, dict) for item in items):
                return None, "account not found"
            parsed = []
            for item in items:
                fields = token_account_fields(item)
                if not fields.get("read"):
                    return None, "account not found"
                parsed.append(fields)
            answered += 1
            rows.extend(parsed)
        if rows:
            return rows, None
        if answered and not errors:
            return None, "account not found"
        return None, self._prefer_rpc_error(errors)

    async def _delegations(self, rpc_url, mint, client, retries):
        """Delegates on holder accounts, from either token program.

        A Token-2022 mint has an empty classic-program list. That empty list
        is not a failed read and must not hide the Token-2022 accounts. When
        one RPC fails, the other public endpoint is tried, then the largest
        accounts. An unread or null account is account not found, not a pass.
        """
        errors = []
        saw_not_found = False
        for url in rpc_urls(rpc_url):
            rows, error = await self._program_holders(url, mint, client, retries)
            if rows:
                return rows, None
            if error == "account not found":
                saw_not_found = True
                continue
            if error:
                errors.append(error)
        for url in rpc_urls(rpc_url):
            rows, error = await self._largest(url, mint, client, retries)
            if rows:
                return rows, None
            if error == "account not found":
                return None, "account not found"
            if error:
                errors.append(error)
        if saw_not_found and not any(error.startswith("rpc") for error in errors):
            return None, "account not found"
        return None, self._prefer_rpc_error(errors) if errors else "account not found"


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
