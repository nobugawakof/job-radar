---
name: telegram-account-feature
description: Build or change anything that uses the owner's own Telegram account through Telethon (sending applications, reading channels, conversation sync, login), with the loop runner, the account lock, timeouts and fake-client tests. Use before touching tgclient.py, apply/senders.py, apply/conversations.py, collectors/telegram.py or web/tglogin.py.
---

# Features on the owner's Telegram account

Telethon is optional and async. This code runs from worker threads and the web
server, and there is a single session file. Follow this pattern every time:

```python
from .. import tgclient

if not tgclient.ACCOUNT_LOCK.acquire(timeout=lock_timeout):   # never wait forever
    return {"error": "Your Telegram account is busy …"}
runner = tgclient.LoopThread("my-feature")
client = None
try:
    client = tgclient.make_client(runner, session_path, api_id, api_hash)   # raises TelethonMissing
    runner.call(lambda: client.connect(), 30)                               # every call has a timeout
    if not runner.call(lambda: client.is_user_authorized(), 15):
        return {"error": "not logged in"}
    ...  # runner.call(lambda: some_coroutine(), timeout)
finally:
    if client is not None:
        try: runner.call(lambda: client.disconnect(), 10)
        except Exception: pass
    runner.stop()                      # joins the thread and closes the loop (no leaked sockets)
    tgclient.ACCOUNT_LOCK.release()
```

Rules:
- Check that the session file exists before connecting. A missing session
  means "not logged in"; don't start a login.
- `ACCOUNT_LOCK` is an RLock, so a sender can hold it across several sends
  (see `TelegramUserSender._connect` / `_unlock`).
- **Async iterators** like `iter_messages`: collect the results inside one
  coroutine that runs under `runner.call`.
- Errors are matched by class name (`type(e).__name__`), so the code also works
  when Telethon isn't installed. `FloodWaitError` and `PeerFloodError` must
  pause applying (see `apply/engine.py`), never retry in a loop.
- Read only the chats the feature needs. The conversation sync reads only
  peers in the `conversations` table.
- Wording a user sees goes through `apply/language.py` (zh and en).

Tests (the sandbox can't reach Telegram):
- Patch `tgclient.make_client` to return a fake with async `connect`,
  `is_user_authorized` and `disconnect`, plus whatever the feature uses. For an
  iterator, return an async generator from a plain method (see `FakeClient` in
  `tests/test_messages.py`).
- Test that a "busy" lock returns an error (hold the lock in another thread
  with `lock_timeout=0.1`), and test the not-logged-in case.
- Tell the owner that this is untested against real Telegram (status.md V2).
