"""In-process queues. Agents talk only through this bus and the database."""

import asyncio


class Bus:
    def __init__(self) -> None:
        self.auditor: asyncio.Queue = asyncio.Queue()
        self.analyst: asyncio.Queue = asyncio.Queue()
        self.executor: asyncio.Queue = asyncio.Queue()
        self.risk: asyncio.Queue = asyncio.Queue()
        self.events: asyncio.Queue = asyncio.Queue()
        self.commands: asyncio.Queue = asyncio.Queue()
        self.waiting: set[str] = set()
        self.open_mints: set[str] = set()
        self.market: dict[str, dict] = {}
