"""Log your own Telegram account in from the dashboard (phone → code → 2FA).

The same flow as ``jobradar telegram-login``, split across three HTTP requests.
The half-logged-in client lives in memory between them; nothing is written
except Telethon's session file, and only once login succeeds.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from ..tgclient import LoopThread, TelethonMissing, make_client, protect_session_file


class LoginError(RuntimeError):
    """A user-facing login problem (wrong code, bad phone, …)."""


def _friendly(e: Exception) -> str:
    name = type(e).__name__
    return {
        "PhoneNumberInvalidError": "That phone number isn't valid. Use the international format, e.g. +60123456789.",
        "PhoneNumberBannedError": "Telegram has banned this phone number.",
        "PhoneCodeInvalidError": "That code is wrong. Check the latest code Telegram sent you.",
        "PhoneCodeExpiredError": "That code expired. Click “Send code” again.",
        "PasswordHashInvalidError": "Wrong two-step verification password.",
        "ApiIdInvalidError": "Telegram rejected the API ID / API hash. Copy them again from my.telegram.org.",
        "FloodWaitError": f"Too many attempts. Telegram asks you to wait {getattr(e, 'seconds', '?')} seconds.",
        "SessionPasswordNeededError": "This account has a two-step verification password.",
    }.get(name, f"{name}: {e}")


class TelegramLogin:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runner: LoopThread | None = None
        self._client: Any = None
        self._phone = ""
        self._hash = ""
        self.stage = "idle"   # idle | code | password

    # ------------------------------------------------------------ helpers
    def _reset(self) -> None:
        if self._client is not None and self._runner is not None:
            try:
                self._runner.call(lambda: self._client.disconnect(), 10)
            except Exception:  # noqa: BLE001
                pass
        if self._runner is not None:
            self._runner.stop()
        self._runner = self._client = None
        self._phone = self._hash = ""
        self.stage = "idle"

    def _call(self, make, timeout: float = 30.0):
        try:
            return self._runner.call(make, timeout)
        except TimeoutError as e:
            raise LoginError(str(e)) from e
        except LoginError:
            raise
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "SessionPasswordNeededError":
                raise
            raise LoginError(_friendly(e)) from e

    def _me(self) -> dict[str, str]:
        me = self._call(lambda: self._client.get_me(), 15)
        return {"username": f"@{me.username}" if getattr(me, "username", None) else "",
                "name": " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x)}

    # ---------------------------------------------------------------- flow
    def start(self, *, api_id: str, api_hash: str, session_path: str, phone: str) -> dict:
        with self._lock:
            self._reset()
            try:
                self._runner = LoopThread("telegram-login")
                self._client = make_client(self._runner, session_path, api_id, api_hash)
            except TelethonMissing as e:
                self._reset()
                raise LoginError(str(e)) from e
            except Exception as e:  # noqa: BLE001
                self._reset()
                raise LoginError(_friendly(e)) from e
            self._call(lambda: self._client.connect(), 30)
            if self._call(lambda: self._client.is_user_authorized(), 15):
                me = self._me()
                self._session_done(session_path)
                return {"stage": "done", **me}
            sent = self._call(lambda: self._client.send_code_request(phone), 30)
            self._phone, self._hash = phone, sent.phone_code_hash
            self.stage = "code"
            return {"stage": "code"}

    def code(self, code: str, session_path: str) -> dict:
        with self._lock:
            if self.stage != "code":
                raise LoginError("Start again: click “Send code” first.")
            try:
                self._call(lambda: self._client.sign_in(phone=self._phone, code=code.strip(),
                                                         phone_code_hash=self._hash), 30)
            except LoginError:
                raise
            except Exception as e:  # noqa: BLE001 - SessionPasswordNeededError
                if type(e).__name__ == "SessionPasswordNeededError":
                    self.stage = "password"
                    return {"stage": "password"}
                raise LoginError(_friendly(e)) from e
            me = self._me()
            self._session_done(session_path)
            return {"stage": "done", **me}

    def password(self, password: str, session_path: str) -> dict:
        with self._lock:
            if self.stage != "password":
                raise LoginError("Start again: click “Send code” first.")
            self._call(lambda: self._client.sign_in(password=password), 30)
            me = self._me()
            self._session_done(session_path)
            return {"stage": "done", **me}

    def _session_done(self, session_path: str) -> None:
        self._reset()
        protect_session_file(session_path)

    # -------------------------------------------------------------- status
    @staticmethod
    def check(*, api_id: str, api_hash: str, session_path: str) -> dict:
        """Connect with the saved session and report who is logged in."""
        from ..tgclient import ACCOUNT_LOCK

        if not ACCOUNT_LOCK.acquire(timeout=15):
            raise LoginError("Your Telegram account is busy (a check is using it). Try again in a minute.")
        runner = LoopThread("telegram-check")
        client = None
        try:
            client = make_client(runner, session_path, api_id, api_hash)
            runner.call(lambda: client.connect(), 30)
            if not runner.call(lambda: client.is_user_authorized(), 15):
                return {"logged_in": False}
            me = runner.call(lambda: client.get_me(), 15)
            return {"logged_in": True,
                    "username": f"@{me.username}" if getattr(me, "username", None) else "",
                    "name": getattr(me, "first_name", "") or ""}
        except TelethonMissing as e:
            raise LoginError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise LoginError(_friendly(e)) from e
        finally:
            if client is not None:
                try:
                    runner.call(lambda: client.disconnect(), 10)
                except Exception:  # noqa: BLE001
                    pass
            runner.stop()
            ACCOUNT_LOCK.release()

    @staticmethod
    def logout(*, api_id: str, api_hash: str, session_path: str) -> None:
        """Log the session out at Telegram (best effort) and delete the file."""
        from ..tgclient import ACCOUNT_LOCK

        path = session_path if session_path.endswith(".session") else session_path + ".session"
        if not ACCOUNT_LOCK.acquire(timeout=30):
            raise LoginError("Your Telegram account is busy (a check is using it). Try again in a minute.")
        try:
            TelegramLogin._logout_locked(api_id=api_id, api_hash=api_hash, session_path=session_path, path=path)
        finally:
            ACCOUNT_LOCK.release()

    @staticmethod
    def _logout_locked(*, api_id: str, api_hash: str, session_path: str, path: str) -> None:
        if os.path.exists(path) and api_id and api_hash:
            runner = LoopThread("telegram-logout")
            try:
                client = make_client(runner, session_path, api_id, api_hash)
                runner.call(lambda: client.connect(), 20)
                runner.call(lambda: client.log_out(), 20)
            except Exception:  # noqa: BLE001 - deleting the file is what matters
                pass
            finally:
                runner.stop()
        for p in (path, path + "-journal"):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
