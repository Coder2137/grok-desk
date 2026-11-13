"""Shared audit readings for the three paper desks.

One file per mint and layer under var/feed/audit-cache. A desk that already
measured a layer leaves the number for the others. Signature pages for one
pool are kept so a later try continues instead of rereading the first page.
Nothing in these files is a secret.
"""

import json
import os
import time
from pathlib import Path

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
LAYER_TTL_S = 600
PAGE_TTL_S = 900


def cache_root() -> Path:
    override = os.environ.get("DESK_AUDIT_CACHE")
    if override:
        return Path(override)
    from feed import feed_path

    return feed_path().parent / "audit-cache"


def _safe_address(value) -> str | None:
    if not isinstance(value, str) or not (32 <= len(value) <= 44):
        return None
    if any(ch not in _ALPHABET for ch in value):
        return None
    return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store_layer(mint: str, layer: int, payload: dict, now: float | None = None) -> None:
    """Remember one layer's measurement. Transient RPC failures are not stored."""
    safe = _safe_address(mint)
    if safe is None or not isinstance(layer, int) or not 1 <= layer <= 8:
        return
    if not isinstance(payload, dict):
        return
    body = {"at": now if now is not None else time.time(), "layer": layer}
    body.update(payload)
    _write_json(cache_root() / safe / f"{layer}.json", body)


def load_layer(mint: str, layer: int, ttl_s: int = LAYER_TTL_S, now: float | None = None) -> dict | None:
    safe = _safe_address(mint)
    if safe is None or not isinstance(layer, int):
        return None
    data = _read_json(cache_root() / safe / f"{layer}.json")
    if not data:
        return None
    moment = now if now is not None else time.time()
    try:
        age = moment - float(data.get("at"))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl_s:
        return None
    return data


def store_pages(address: str, signatures: list, before: str | None, done: bool, now: float | None = None) -> None:
    safe = _safe_address(address)
    if safe is None:
        return
    rows = [row for row in signatures if isinstance(row, dict)]
    _write_json(
        cache_root() / "pages" / f"{safe}.json",
        {
            "at": now if now is not None else time.time(),
            "before": before,
            "done": bool(done),
            "signatures": rows,
        },
    )


def load_pages(address: str, ttl_s: int = PAGE_TTL_S, now: float | None = None) -> dict | None:
    safe = _safe_address(address)
    if safe is None:
        return None
    data = _read_json(cache_root() / "pages" / f"{safe}.json")
    if not data or not isinstance(data.get("signatures"), list):
        return None
    moment = now if now is not None else time.time()
    try:
        age = moment - float(data.get("at"))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl_s:
        return None
    return data


def acquire(name: str, stale_s: int = 90) -> bool:
    """One desk scans a pool. A lock older than stale_s is dropped."""
    safe = _safe_address(name)
    if safe is None:
        return False
    path = cache_root() / "locks" / f"{safe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and time.time() - path.stat().st_mtime > stale_s:
            path.unlink()
    except OSError:
        return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.close(fd)
    return True


def release(name: str) -> None:
    safe = _safe_address(name)
    if safe is None:
        return
    path = cache_root() / "locks" / f"{safe}.lock"
    try:
        path.unlink()
    except OSError:
        return
