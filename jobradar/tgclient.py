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


# Your account's session file must not be opened by two clients at once (it's
# a SQLite file; parallel use corrupts or locks it). Every user of the account
# — sender, channel reader, conversation sync, login check — holds this lock.
ACCOUNT_LOCK = threading.RLock()


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
        """Stop the loop and release its resources (each use of the account
        makes one; a dashboard open for weeks must not leak them)."""
        if self.loop.is_closed():
            return
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        if not self._thread.is_alive():
            # Cancel whatever Telethon left scheduled, then close the loop.
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
            try:
                self.loop.run_until_complete(asyncio.sleep(0))
            except Exception:  # noqa: BLE001
                pass
            self.loop.close()


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
