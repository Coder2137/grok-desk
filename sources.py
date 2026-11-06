"""HTTP reads for DexScreener, RugCheck, and Solana RPC. No orders, no FOMO calls."""

import time
from collections import deque

import httpx

DEX = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
RUG_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUG_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

_DEX_TIMES: deque[float] = deque()


class DexRateLimit(Exception):
    pass


class RpcRefused(Exception):
    pass


def _note_dex(count: int = 1) -> None:
    now = time.monotonic()
    while _DEX_TIMES and now - _DEX_TIMES[0] > 60:
        _DEX_TIMES.popleft()
    if len(_DEX_TIMES) + count > 250:
        raise DexRateLimit("dexscreener minute cap")
    for _ in range(count):
        _DEX_TIMES.append(now)


async def dexscreener_batch(mints: list[str], client: httpx.AsyncClient | None = None) -> list:
    """One batch per call. Caller must not fire a second source for the same write."""
    clean = [m for m in mints if m]
    if not clean:
        return []
    own = client is None
    http = client or httpx.AsyncClient(timeout=15)
    try:
        out = []
        for start in range(0, len(clean), 30):
            chunk = clean[start : start + 30]
            _note_dex(1)
            url = DEX.format(mints=",".join(chunk))
            response = await http.get(url)
            if response.status_code == 429:
                raise DexRateLimit("429")
            response.raise_for_status()
            body = response.json()
            if isinstance(body, list):
                out.extend(body)
            elif isinstance(body, dict):
                for value in body.values():
                    if isinstance(value, list):
                        out.extend(value)
        return out
    finally:
        if own:
            await http.aclose()


async def rug_report(mint: str, client: httpx.AsyncClient) -> dict | None:
    response = await client.get(RUG_REPORT.format(mint=mint), timeout=15)
    if response.status_code >= 400:
        return None
    body = response.json()
    return body if isinstance(body, dict) else None


async def rug_graph(mint: str, client: httpx.AsyncClient):
    response = await client.get(RUG_GRAPH.format(mint=mint), timeout=15)
    if response.status_code >= 400:
        return None
    return response.json()


async def rpc(url: str, method: str, params, client: httpx.AsyncClient):
    response = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    if response.status_code in (401, 403, 429):
        if method == "getProgramAccounts":
            raise RpcRefused(f"http {response.status_code}")
        return None
    body = response.json()
    if isinstance(body, dict) and body.get("error"):
        message = str(body["error"].get("message", "")) if isinstance(body["error"], dict) else str(body["error"])
        code = body["error"].get("code") if isinstance(body["error"], dict) else None
        if method == "getProgramAccounts" and (
            code in (-32601, -32011, 403) or "not allowed" in message.lower() or "excluded" in message.lower()
            or "unavailable" in message.lower() or "forbidden" in message.lower()
        ):
            raise RpcRefused(message)
        return None
    return body
