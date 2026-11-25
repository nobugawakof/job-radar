"""Senders: deliver one application to one contact.

Two channels:

* **telegram** — your *own* Telegram account via the MTProto user API
  (Telethon). The Bot API can't message people who never started your bot, so a
  user session is the only way to DM a hiring manager. Telethon is an optional
  dependency (``pip install telethon``); log in once with ``jobradar
  telegram-login``.
* **email** — plain SMTP from the standard library (STARTTLS or implicit TLS).

Errors are split by what the caller should do next:

* :class:`ContactError` — this one contact failed (no such user, privacy,
  rejected address). Try another channel; don't retry this contact.
* :class:`ChannelUnavailable` — the whole channel must stop (Telegram flood
  limits, not logged in, SMTP login failed). ``pause_seconds`` > 0 means "don't
  use this channel again until then"; 0 means "skip it for this run only".
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Protocol

log = logging.getLogger("jobradar.apply")


class ContactError(RuntimeError):
    """Sending to this particular contact failed."""


class ChannelUnavailable(RuntimeError):
    """The whole channel can't be used right now."""

    def __init__(self, message: str, pause_seconds: float = 0.0):
        super().__init__(message)
        self.pause_seconds = pause_seconds


class Sender(Protocol):
    channel: str

    def send(self, contact: str, *, message: str, subject: str,
             attachment: Path | None) -> None: ...

    def close(self) -> None: ...


# ----------------------------------------------------------------- Telegram
# Telegram's flood control on messaging non-contacts is strict and the penalty
# for ignoring it is an account restriction, so a PEER_FLOOD stops the channel
# for a full day.
PEER_FLOOD_PAUSE_SECONDS = 24 * 3600


class TelegramUserSender:
    channel = "telegram"

    def __init__(self, *, api_id: str, api_hash: str, session_path: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self._client = None

    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            from telethon.sync import TelegramClient
        except ImportError as e:
            raise ChannelUnavailable("Telegram auto-apply needs Telethon: pip install telethon") from e
        try:
            client = TelegramClient(self.session_path, int(self.api_id), self.api_hash)
            client.connect()
        except Exception as e:  # noqa: BLE001 - network/session problems
            raise ChannelUnavailable(f"could not connect your Telegram account: {e}") from e
        if not client.is_user_authorized():
            client.disconnect()
            raise ChannelUnavailable("your Telegram account is not logged in — run "
                                     "`jobradar telegram-login` once")
        self._client = client
        return client

    def send(self, contact: str, *, message: str, subject: str = "",
             attachment: Path | None = None) -> None:
        from telethon import errors

        client = self._connect()
        try:
            entity = client.get_entity(contact)
            client.send_message(entity, message, link_preview=False)
        except errors.FloodWaitError as e:
            raise ChannelUnavailable(f"Telegram flood wait {e.seconds}s",
                                     pause_seconds=max(float(e.seconds), 3600.0)) from e
        except errors.PeerFloodError as e:
            raise ChannelUnavailable(
                "Telegram says this account is messaging too many strangers (PEER_FLOOD). "
                "Paused for 24h — check @SpamBot in Telegram for account limits.",
                pause_seconds=PEER_FLOOD_PAUSE_SECONDS) from e
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                errors.UserDeactivatedError, errors.UserDeactivatedBanError) as e:
            raise ChannelUnavailable(f"your Telegram session is no longer valid ({e}); run "
                                     "`jobradar telegram-login` again",
                                     pause_seconds=PEER_FLOOD_PAUSE_SECONDS) from e
        except (ValueError, errors.RPCError) as e:
            # ValueError: username doesn't resolve. RPCError: privacy settings,
            # blocked, deactivated user, etc. — this contact only.
            raise ContactError(f"Telegram {contact}: {e}") from e

        if attachment:
            try:
                client.send_file(entity, str(attachment), force_document=True)
            except Exception as e:  # noqa: BLE001 - message already went out
                log.warning("message sent to %s but the résumé upload failed: %s", contact, e)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            finally:
                self._client = None


# -------------------------------------------------------------------- Email
class EmailSender:
    channel = "email"

    def __init__(self, *, host: str, port: int, user: str | None, password: str | None,
                 sender: str, use_ssl: bool = False, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._smtp: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        if self._smtp is not None:
            return self._smtp
        ctx = ssl.create_default_context()
        try:
            if self.use_ssl:
                smtp: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                smtp.starttls(context=ctx)
            if self.user:
                smtp.login(self.user, self.password or "")
        except smtplib.SMTPAuthenticationError as e:
            raise ChannelUnavailable(f"SMTP login failed for {self.user} — check smtp_user / "
                                     "smtp_password (Gmail needs an App Password)") from e
        except (OSError, smtplib.SMTPException) as e:
            raise ChannelUnavailable(f"could not connect to SMTP {self.host}:{self.port}: {e}") from e
        self._smtp = smtp
        return smtp

    def build(self, to: str, *, subject: str, body: str, attachment: Path | None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1])
        msg.set_content(body)
        if attachment:
            ctype = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=attachment.name)
        return msg

    def send(self, contact: str, *, message: str, subject: str,
             attachment: Path | None = None) -> None:
        msg = self.build(contact, subject=subject, body=message, attachment=attachment)
        smtp = self._connect()
        try:
            smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise ContactError(f"email {contact} refused: {e.recipients}") from e
        except smtplib.SMTPDataError as e:
            if 400 <= e.smtp_code < 500 or e.smtp_code in (550, 554) and b"rate" in (e.smtp_error or b"").lower():
                raise ChannelUnavailable(f"SMTP is throttling us ({e.smtp_code}); pausing email",
                                         pause_seconds=3600.0) from e
            raise ContactError(f"email {contact} rejected: {e.smtp_code} {e.smtp_error!r}") from e
        except (smtplib.SMTPServerDisconnected, OSError) as e:
            self._smtp = None
            raise ChannelUnavailable(f"SMTP connection lost: {e}") from e

    def close(self) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._smtp = None
