"""Run Telethon (your own Telegram account) safely from any thread.

Telethon is asyncio-based. Rather than its ``telethon.sync`` shim — which runs
coroutines on whatever thread calls it and can hang a run forever when the
network stalls — every client gets a private event loop on its own daemon
thread, and every call is bounded by a hard timeout. That makes it safe to use
from the dashboard's worker threads and from the pipeline alike.

Telethon is optional (``pip install telethon``); import errors surface as
:class:`TelethonMissing` so callers can show a friendly message.
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Awaitable, Callable


class TelethonMissing(RuntimeError):
    pass


def require_telethon():
    try:
        import telethon  # noqa: F401
    except ImportError as e:
        raise TelethonMissing("Telegram account features need Telethon: pip install telethon") from e
    return telethon


class LoopThread:
    """A private asyncio loop on a daemon thread; ``call`` runs one coroutine
    on it with a hard timeout and returns the result (or raises)."""

    def __init__(self, name: str = "telethon"):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self._thread.start()

    def call(self, make: Callable[[], Awaitable[Any]], timeout: float = 30.0) -> Any:
        # The coroutine is created *inside* the loop thread, so Telethon binds
        # to this loop even if ``telethon.sync`` was imported elsewhere.
        async def runner():
            return await asyncio.wait_for(make(), timeout)

        fut = asyncio.run_coroutine_threadsafe(runner(), self.loop)
        try:
            return fut.result(timeout + 5)
        except (FutureTimeout, asyncio.TimeoutError) as e:
            fut.cancel()
            raise TimeoutError(f"Telegram did not respond within {timeout:.0f}s") from e

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(5)


def make_client(runner: LoopThread, session_path: str, api_id: str | int, api_hash: str):
    """Create a TelegramClient bound to ``runner``'s loop."""
    require_telethon()
    from telethon import TelegramClient

    async def make():
        return TelegramClient(session_path, int(api_id), api_hash)

    return runner.call(make, 10)


def protect_session_file(session_path: str) -> None:
    """The session file is full access to the account: owner-only permissions."""
    path = session_path if session_path.endswith(".session") else session_path + ".session"
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except OSError:
        pass
