"""One local migration feed. Desk 1 appends. Desk 2 reads. No second websocket."""

import json
import os
from pathlib import Path

_MAX_BYTES = 8_000_000


def feed_path() -> Path:
    return Path(os.environ.get("DESK_FEED") or "var/feed/migrations.jsonl")


def offset_path() -> Path:
    return Path(os.environ.get("DESK_FEED_OFFSET") or "var/desk2/feed.offset")


def append_migration(message: dict) -> None:
    """Append one frame. A full disk or a missing directory must not stop desk 1."""
    try:
        path = feed_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > _MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
        line = json.dumps(message, default=str, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return


def read_new(offset: int) -> tuple[list[str], int]:
    path = feed_path()
    if not path.is_file():
        return [], 0
    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    if not data:
        return [], offset
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset
        data = data[: cut + 1]
    lines = []
    for raw in data.split(b"\n"):
        if raw:
            lines.append(raw.decode("utf-8", errors="replace"))
    return lines, offset + len(data)


def load_offset() -> int:
    try:
        return int(offset_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def store_offset(offset: int) -> None:
    path = offset_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(offset)), encoding="utf-8")
