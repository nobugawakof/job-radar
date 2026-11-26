"""The dashboard: a local web page for setting up and watching Job Radar.

    jobradar web            # opens http://127.0.0.1:8765 in your browser

Standard library only (``http.server``). It replaces hand-editing
``config.toml``: every setting has a form, every connection a "Test" button,
and it runs the schedule itself (once you switch "check automatically" on).

Security — the page can read and change your tokens, so:

* it listens on 127.0.0.1 only, unless you pass ``--host`` explicitly;
* every request needs a secret session cookie, issued when you open the link
  the program prints (the link carries a one-time-looking token stored in
  ``jobradar-web.token`` next to your config, so bookmarks keep working);
* requests with a foreign ``Host`` header are refused (DNS-rebinding), and
  changes require a custom header and a same-origin ``Origin`` (CSRF);
* secrets are never sent to the browser — only "set (…1234)".
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..config import Config, load
from ..redact import redact
from ..service import Service
from ..state import iso, parse_iso, utcnow
from . import configfile as cf
from .tglogin import LoginError, TelegramLogin

log = logging.getLogger("jobradar.web")

COOKIE = "jobradar_session"
INDEX = Path(__file__).with_name("index.html")
MAX_JSON = 1_000_000
MAX_RESUME = 10_000_000

KNOWN_SITES = {
    "en": [
        {"url": "news.ycombinator.com", "label": "Hacker News", "note": "monthly “Who is hiring?” thread"},
        {"url": "weworkremotely.com", "label": "We Work Remotely", "note": "remote programming jobs"},
        {"url": "reddit.com/r/forhire", "label": "r/forhire", "note": "Reddit"},
        {"url": "reddit.com/r/remotejs", "label": "r/remotejs", "note": "remote JavaScript"},
        {"url": "reddit.com/r/remotepython", "label": "r/remotepython", "note": "remote Python"},
        {"url": "reddit.com/r/RemoteJobs", "label": "r/RemoteJobs", "note": "general remote"},
        {"url": "bsky.app", "label": "Bluesky", "note": "search “hiring remote”"},
    ],
    "cn": [
        {"url": "v2ex.com", "label": "V2EX", "note": "酷工作 + 远程"},
        {"url": "ruby-china.org", "label": "Ruby China", "note": "topics feed"},
    ],
}


def _placeholder(v: Any) -> bool:
    return isinstance(v, str) and (not v.strip() or v.strip().upper().startswith("PASTE"))


# ------------------------------------------------------------------- logs
class LogBuffer(logging.Handler):
    """Keeps the last few hundred log lines (secrets redacted) for the page."""

    def __init__(self, size: int = 400):
        super().__init__(level=logging.INFO)
        self.lines: deque[dict[str, Any]] = deque(maxlen=size)
        self.seq = 0
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                msg += f" ({record.exc_info[1]})"
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        with self._lock2:
            self.seq += 1
            self.lines.append({"seq": self.seq, "t": iso(utcnow()), "level": record.levelname,
                               "src": record.name.replace("jobradar.", ""), "msg": redact(msg)[:1000]})

    def since(self, after: int) -> list[dict[str, Any]]:
        with self._lock2:
            return [x for x in self.lines if x["seq"] > after]


# ------------------------------------------------------------- controller
class Controller:
    """Owns the Service, runs it in the background, and backs the API."""

    def __init__(self, config_path: Path, logs: LogBuffer | None = None):
        self.config_path = config_path.resolve()
        self.base_dir = self.config_path.parent
        self.logs = logs or LogBuffer()
        self.login = TelegramLogin()
        self._svc_lock = threading.RLock()
        self.service: Service = self._build(None)
        self.running = False
        self.run_started: str | None = None
        self.last_summary: dict[str, Any] | None = None
        self._manual = False
        self._last_sync = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ service
    def _build(self, old: Service | None) -> Service:
        cfg = load(self.config_path)
        return Service(cfg, config_path=str(self.config_path), state=old.state if old else None)

    def reload(self) -> None:
        with self._svc_lock:
            old = self.service
            self.service = self._build(old)
            if old.inbox is not None and old.inbox is not self.service.inbox:
                old.inbox.stop()  # the bot token changed
            if self._thread is not None and self.service.inbox is not None:
                self.service.inbox.start()

    def restore(self, data: bytes) -> dict[str, Any]:
        """Replace settings, database and files with a backup's (see
        jobradar/backup.py). Refuses while a check is running."""
        from ..backup import read_backup, restore_backup

        read_backup(data)  # validate before touching anything
        with self._svc_lock:
            if self.running:
                raise ApiError("Wait for the current check to finish, then restore.", 409)
            old = self.service
            if not old.state.acquire_run_lock("restore"):
                raise ApiError("Another Job Radar is checking right now; close it first.", 409)
            if old.inbox is not None:
                old.inbox.stop()
            old.state.close()
            try:
                result = restore_backup(data, self.config_path)
            finally:
                self.service = self._build(None)  # fresh database connection
                self.last_summary = None
                if self._thread is not None and self.service.inbox is not None:
                    self.service.inbox.start()
        log.info("restored a backup from %s", result.get("backup_created"))
        return result

    @property
    def config(self) -> Config:
        return self.service.config

    def configured(self) -> bool:
        c = self.config
        return not _placeholder(c.telegram_bot_token) and not _placeholder(c.telegram_chat_id)

    def resolve(self, p: str | None) -> Path:
        path = Path(p or "").expanduser()
        return path if path.is_absolute() else self.base_dir / path

    # ------------------------------------------------------------- runner
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="jobradar-runner", daemon=True)
        self._thread.start()
        if self.service.inbox is not None:
            self.service.inbox.start()  # Telegram buttons and channel posts

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.service.inbox is not None:
            self.service.inbox.stop()

    def request_run(self) -> bool:
        if self.running:
            return False
        self._manual = True
        self._wake.set()
        return True

    def next_run(self) -> str | None:
        c = self.config
        if not (c.auto_run and self.configured()):
            return None
        last = parse_iso(self.service.state.last_run)
        return iso(last + timedelta(hours=c.run_interval_hours)) if last else iso(utcnow())

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=20)
            self._wake.clear()
            if self._stop.is_set():
                break
            manual, self._manual = self._manual, False
            if time.time() - self._last_sync > 300 and not self.running:
                self._last_sync = time.time()
                with self._svc_lock:
                    svc = self.service
                if svc.state.conversations("telegram"):
                    svc.sync_conversations()
            nxt = self.next_run()
            due = nxt is not None and parse_iso(nxt) <= utcnow()
            if manual or due:
                self._run_once("manual" if manual else "scheduled")

    def _run_once(self, trigger: str) -> None:
        with self._svc_lock:
            svc = self.service
        if not self.configured():
            log.warning("Set up the Telegram bot first (Telegram tab) — nothing to send jobs to.")
            return
        self.running, self.run_started = True, iso(utcnow())
        log.info("checking for new jobs (%s)…", trigger)
        try:
            s = svc._safe_run(trigger)
            if s is not None:
                self.last_summary = summarize(s)
        finally:
            self.running = False


def summarize(s) -> dict[str, Any]:
    return s.to_dict()


# ------------------------------------------------------------------ state
_QR_CACHE: dict[str, str] = {}


def _network(ctrl: Controller) -> dict[str, Any]:
    """Whether other devices can open the dashboard, and the links (with QR
    codes) to do it. Only ever sent to a logged-in browser."""
    net = getattr(ctrl, "network", None) or {}
    allow = bool(ctrl.config.web_allow_network)
    out: dict[str, Any] = {"allow": allow, "listening": bool(net.get("listening_all")),
                           "forced": bool(net.get("forced")), "addresses": []}
    out["restart_needed"] = not out["forced"] and allow != out["listening"]
    if out["listening"]:
        from . import netinfo, qr

        for a in netinfo.addresses():
            url = f"http://{a['ip']}:{net['port']}/?t={net['token']}"
            if url not in _QR_CACHE:
                _QR_CACHE[url] = qr.svg(url, scale=4)
            out["addresses"].append({**a, "url": url, "qr": _QR_CACHE[url]})
    return out


def _profile_complete(st: Any) -> bool:
    from ..resume import profile as prof

    return prof.is_complete(st.get_profile())


def build_state(ctrl: Controller) -> dict[str, Any]:
    c = ctrl.config
    raw = cf.read_raw(ctrl.config_path)
    st = ctrl.service.state
    values: dict[str, Any] = {}
    secrets_info: dict[str, Any] = {}
    for key in Config.__dataclass_fields__:
        v = getattr(c, key)
        if key in cf.SECRET_KEYS:
            in_file = key in raw and not _placeholder(raw.get(key))
            env = bool(os.environ.get(cf.ENV_FOR.get(key, ""), ""))
            secrets_info[key] = {"set": bool(in_file or env), "env": env,
                                 "hint": cf.mask(v) if (in_file or env) and v else ""}
        else:
            values[key] = "" if _placeholder(v) and key == "telegram_chat_id" else v
    pdf = ctrl.resolve(c.resume_pdf_path or "resume.pdf")
    txt = ctrl.resolve(c.resume_text_path) if c.resume_text_path else None
    session = ctrl.resolve(c.telegram_session_path)
    session_file = session if str(session).endswith(".session") else Path(str(session) + ".session")
    sources = []
    known = st.sources_info()
    for sdef in ctrl.service.source_defs:
        info = known.get(sdef["name"], {})
        sources.append({"name": sdef["name"], "type": sdef["type"], "tier": sdef.get("tier", "A"),
                        "url": sdef.get("url", ""), "disabled": bool(info.get("disabled")),
                        "failures": info.get("failures", 0), "last_success": info.get("last_success")})
    counts = st.application_counts()  # sent / dry_run / skipped_fit / skipped_cooldown / failed
    has_sites = bool(c.en or c.cn or raw.get("sources"))
    checklist = [
        {"id": "telegram", "label": "Connect your Telegram bot", "done": ctrl.configured(), "tab": "telegram"},
        {"id": "search", "label": "Choose sites and keywords", "tab": "search",
         "done": has_sites and "keywords" in raw},
        {"id": "autorun", "label": "Switch on automatic checks", "done": bool(c.auto_run), "tab": "overview"},
    ]
    if (c.apply_mode or "off") != "off" and (c.resume_source or "generated") == "generated":
        checklist.append({"id": "profile", "label": "Fill in your profile (your résumé is made from it)",
                          "done": _profile_complete(st), "tab": "profile"})
    return {
        "version": __version__,
        "config_file": str(ctrl.config_path),
        "data_folder": str(ctrl.base_dir),
        "configured": ctrl.configured(),
        "running": ctrl.running, "run_started": ctrl.run_started,
        "last_run": st.last_run, "next_run": ctrl.next_run(),
        "last_summary": _newest(ctrl.last_summary, st.last_run_summary()),
        "history": st.recent_runs(10),
        "unread_messages": st.unread_total(),
        "network": _network(ctrl),
        "review": {"pending": st.review_counts().get("pending", 0), "mode": c.unknown_location,
                   "regions_set": bool(c.regions)},
        "checklist": checklist,
        "config": values, "secrets": secrets_info,
        "known_sites": KNOWN_SITES,
        "sources": sources, "using_default_sources": not has_sites,
        "recent": st.recent_jobs(50), "sent_total": st.sent_count(),
        "apply": {
            "counts": counts, "queue": st.queue_len(),
            "paused": {k: v for k, v in st.paused_channels().items() if parse_iso(v) and parse_iso(v) > utcnow()},
            "resume_pdf": {"exists": pdf.exists(), "name": pdf.name,
                           "size": pdf.stat().st_size if pdf.exists() else 0},
            "resume_text": txt.read_text("utf-8")[:20000] if txt and txt.exists() else "",
            "resume_source": c.resume_source or "generated",
            "profile_complete": _profile_complete(st),
            "telegram_session": session_file.exists(),
            "login_stage": ctrl.login.stage,
            "telethon": _has_telethon(),
        },
    }


def _newest(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
    """The more recent of two run summaries (this process's, or the stored
    one, which may come from another program sharing the database)."""
    if not a or not b:
        return a or b
    return a if (a.get("at") or "") >= (b.get("at") or "") else b


def _has_telethon() -> bool:
    import importlib.util

    return importlib.util.find_spec("telethon") is not None


# ------------------------------------------------------------ bot helpers
def bot_call(token: str, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except (json.JSONDecodeError, ValueError):
            data = {"description": f"HTTP {e.code}"}
        if e.code in (401, 404):
            raise cf.ConfigError("Telegram doesn't recognise that bot token. Copy it again from @BotFather.") from e
        raise cf.ConfigError(redact(f"Telegram: {data.get('description')}")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise cf.ConfigError(f"Couldn't reach Telegram: {redact(str(getattr(e, 'reason', e)))}") from e
    if not data.get("ok"):
        raise cf.ConfigError(redact(f"Telegram: {data.get('description')}"))
    return data.get("result")


def find_chats(token: str, state) -> dict[str, Any]:
    """The bot's name and every chat that has written to it. Chats are read
    through the bot inbox, which remembers them, so they're still found after
    the updates were read (by the background inbox, or by an earlier check)."""
    from ..delivery.inbox import InboxError, inbox_for

    me = bot_call(token, "getMe")
    try:
        inbox_for(token, state).poll()
    except InboxError as e:
        msg = str(e).lower()
        if "webhook" in msg:
            raise cf.ConfigError("This bot has a webhook set, so its messages can't be read here. "
                                 "Remove the webhook or use a new bot.") from e
        if "409" not in msg:  # 409: another program is reading this bot; its chats are still stored
            raise cf.ConfigError(str(e)) from e
    return {"bot": {"username": me.get("username", ""), "name": me.get("first_name", "")},
            "chats": state.bot_chats()}


# ---------------------------------------------------------------- the API
class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _require(cond: Any, message: str, status: int = 400) -> None:
    if not cond:
        raise ApiError(message, status)


class Api:
    def __init__(self, ctrl: Controller):
        self.ctrl = ctrl

    # Each handler gets the parsed JSON body (or raw bytes for uploads).
    def routes(self) -> dict[tuple[str, str], Callable[[Any], Any]]:
        return {
            ("GET", "/api/state"): lambda _: build_state(self.ctrl),
            ("GET", "/api/applications"): self.applications,
            ("GET", "/api/review"): self.review_list,
            ("GET", "/api/messages"): self.messages_list,
            ("POST", "/api/messages/open"): self.messages_open,
            ("POST", "/api/messages/sync"): self.messages_sync,
            ("POST", "/api/messages/send"): self.messages_send,
            ("POST", "/api/review/decide"): self.review_decide,
            ("POST", "/api/config"): self.save_config,
            ("POST", "/api/run"): self.run,
            ("POST", "/api/telegram/find"): self.telegram_find,
            ("POST", "/api/telegram/test"): self.telegram_test,
            ("POST", "/api/ai/test"): self.ai_test,
            ("POST", "/api/email/test"): self.email_test,
            ("POST", "/api/apply/preview"): self.apply_preview,
            ("PUT", "/api/resume"): self.resume_upload,
            ("GET", "/api/profile"): self.profile_get,
            ("POST", "/api/profile"): self.profile_save,
            ("PUT", "/api/profile/import"): self.profile_import,
            ("POST", "/api/profile/import"): self.profile_import_saved,
            ("POST", "/api/profile/preview"): self.profile_preview,
            ("PUT", "/api/restore"): self.restore,
            ("DELETE", "/api/resume"): self.resume_delete,
            ("POST", "/api/resume/text"): self.resume_text,
            ("POST", "/api/tg/start"): self.tg_start,
            ("POST", "/api/tg/code"): self.tg_code,
            ("POST", "/api/tg/password"): self.tg_password,
            ("POST", "/api/tg/check"): self.tg_check,
            ("POST", "/api/tg/logout"): self.tg_logout,
            ("POST", "/api/sources/enable"): self.source_enable,
        }

    def _save(self, changes: dict[str, Any]) -> None:
        raw = cf.read_raw(self.ctrl.config_path)
        cf.write(self.ctrl.config_path, cf.apply_changes(raw, changes))
        self.ctrl.reload()

    # ---------------------------------------------------------- settings
    def save_config(self, body: dict[str, Any]) -> dict[str, Any]:
        changes = body.get("changes")
        _require(isinstance(changes, dict) and changes, "nothing to save")
        changes = {k: v for k, v in changes.items() if not (k in cf.SECRET_KEYS and v == "")}
        if changes.get("apply_mode") == "auto":
            pdf = self.ctrl.resolve(self.ctrl.config.resume_pdf_path or "resume.pdf")
            _require(pdf.exists(), "Upload your résumé PDF before switching auto-apply on.")
        try:
            self._save(changes)
        except cf.ConfigError as e:
            raise ApiError(str(e)) from e
        log.info("settings saved: %s", ", ".join(sorted(changes)))
        return build_state(self.ctrl)

    def messages_list(self, _: Any) -> dict[str, Any]:
        st = self.ctrl.service.state
        return {"conversations": st.conversations(), "unread": st.unread_total(),
                "telegram_logged_in": build_state(self.ctrl)["apply"]["telegram_session"]}

    def messages_open(self, body: dict[str, Any]) -> dict[str, Any]:
        st = self.ctrl.service.state
        try:
            conv_id = int(body.get("id"))
        except (TypeError, ValueError) as e:
            raise ApiError("missing conversation id") from e
        conv = st.conversation(conv_id)
        _require(conv, "That conversation doesn't exist.", 404)
        st.mark_read(conv_id)
        conv["unread"] = 0
        return {"conversation": conv}

    def messages_sync(self, body: dict[str, Any]) -> dict[str, Any]:
        _require(not self.ctrl.running, "A check is running; it reads new replies itself when done.", 409)
        result = self.ctrl.service.sync_conversations()
        self.ctrl._last_sync = time.time()
        return {"result": result, **self.messages_list(None)}

    def messages_send(self, body: dict[str, Any]) -> dict[str, Any]:
        from ..apply.conversations import ReplyError, send_reply

        try:
            conv_id = int(body.get("id"))
        except (TypeError, ValueError) as e:
            raise ApiError("missing conversation id") from e
        try:
            conv = send_reply(self.ctrl.config, self.ctrl.service.state, self.ctrl.base_dir, conv_id,
                              str(body.get("text") or ""))
        except ReplyError as e:
            raise ApiError(str(e)) from e
        log.info("reply sent in a conversation")
        return {"conversation": conv}

    def review_list(self, _: Any) -> dict[str, Any]:
        st = self.ctrl.service.state
        suggestions = []
        for source, c in sorted(st.review_by_source().items()):
            if c["rejected"] >= 5 and c["approved"] == 0:
                suggestions.append({"source": source, "rejected": c["rejected"], "text":
                    f"You've skipped all {c['rejected']} jobs from {source} that didn't say where they hire. "
                    f"Switch {source} off in Job search, or set those jobs to Drop."})
        return {"pending": st.review_pending(), "counts": st.review_counts(), "suggestions": suggestions}

    def review_decide(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            item_id = int(body.get("id"))
        except (TypeError, ValueError) as e:
            raise ApiError("missing job id") from e
        msg = self.ctrl.service.review_decide(item_id, bool(body.get("approve")), "dashboard")
        return {"message": msg, **self.review_list(None)}

    def applications(self, _: Any) -> dict[str, Any]:
        st = self.ctrl.service.state
        queue = [{"title": e.get("title"), "telegram": e.get("telegram"), "email": e.get("email"),
                  "posted_at": e.get("posted_at"), "url": e.get("source_url")}
                 for _, e in st.queue_items()]
        return {"applications": st.list_applications(300), "queue": queue}

    def run(self, _: Any) -> dict[str, Any]:
        _require(self.ctrl.configured(), "Connect your Telegram bot first (Telegram tab).")
        _require(self.ctrl.request_run(), "A check is already running.", 409)
        time.sleep(0.2)
        return build_state(self.ctrl)

    def source_enable(self, body: dict[str, Any]) -> dict[str, Any]:
        _require(not self.ctrl.running, "Wait for the current check to finish.", 409)
        name = str(body.get("name") or "")
        st = self.ctrl.service.state
        _require(name in st.sources_info(), "unknown source")
        st.enable_source(name)
        return build_state(self.ctrl)

    # ---------------------------------------------------------- telegram
    def telegram_find(self, body: dict[str, Any]) -> dict[str, Any]:
        token = str(body.get("token") or "").strip() or (self.ctrl.config.telegram_bot_token or "")
        _require(token and not _placeholder(token), "Paste your bot token first.")
        try:
            found = find_chats(token, self.ctrl.service.state)
        except cf.ConfigError as e:
            raise ApiError(str(e)) from e
        if body.get("token"):
            self._save({"telegram_bot_token": token})
        return found

    def telegram_test(self, _: Any) -> dict[str, Any]:
        _require(self.ctrl.configured(), "Save a bot token and choose your chat first.")
        try:
            self.ctrl.service.notify_owner("✅ Job Radar is connected. New jobs will arrive here.")
        except Exception as e:  # noqa: BLE001
            raise ApiError(redact(str(e))) from e
        return {"message": "Test message sent — check Telegram."}

    # ---------------------------------------------------------------- AI
    def ai_test(self, _: Any) -> dict[str, Any]:
        from .. import ai
        from ..apply.engine import ai_key

        c = self.ctrl.config
        ok, msg = ai.check(provider=c.ai_provider, api_key=ai_key(c) or "", model=c.ai_model)
        _require(ok, msg)
        return {"message": msg}

    # ------------------------------------------------------------- email
    def email_test(self, _: Any) -> dict[str, Any]:
        from ..apply.engine import build_senders
        from ..apply.senders import ChannelUnavailable

        sender = build_senders(self.ctrl.config, self.ctrl.base_dir).get("email")
        _require(sender, "Fill in the SMTP server and your email address first.")
        try:
            sender._connect()
        except ChannelUnavailable as e:
            raise ApiError(str(e)) from e
        finally:
            sender.close()
        return {"message": "Connected and logged in to your mail server."}

    # -------------------------------------------------------- auto-apply
    def apply_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        from ..apply.engine import build_applier
        from ..cli import _SAMPLE_POSTING
        from ..extraction import extract_title

        text = str(body.get("posting") or "").strip() or _SAMPLE_POSTING
        c = self.ctrl.config
        applier = build_applier(c, self.ctrl.service.state, base_dir=self.ctrl.base_dir)
        draft = applier.drafter({"title": extract_title(text), "description": text})
        _require(draft, "Couldn't write a draft: the AI call failed (test it in the AI tab).")
        return {"source": draft.source, "fit_score": draft.fit_score, "fit_reason": draft.fit_reason,
                "message": draft.message, "email_subject": draft.email_subject,
                "email_body": draft.email_body, "threshold": c.apply_min_score,
                "ai": applier.ai_available}

    def restore(self, data: bytes) -> dict[str, Any]:
        from ..backup import BackupError

        try:
            result = self.ctrl.restore(data)
        except BackupError as e:
            raise ApiError(str(e)) from e
        return {"result": result, **build_state(self.ctrl)}

    # ------------------------------------------------------------ profile
    def profile_get(self, _: Any) -> dict[str, Any]:
        from ..resume import profile as prof

        p = prof.normalize(self.ctrl.service.state.get_profile())
        return {"profile": p, "complete": prof.is_complete(p)}

    def profile_save(self, body: dict[str, Any]) -> dict[str, Any]:
        from ..resume import profile as prof

        try:
            p = prof.validate(body.get("profile") if isinstance(body.get("profile"), dict) else {})
        except prof.ProfileError as e:
            raise ApiError(str(e)) from e
        self.ctrl.service.state.set_profile(p)
        log.info("profile saved")
        return {"profile": p, "complete": True, **build_state(self.ctrl)}

    def _ai(self) -> tuple[str, str]:
        from ..apply.engine import ai_key

        c = self.ctrl.config
        key = ai_key(c)
        _require(key, "Add an AI key on the AI page first: reading a résumé needs AI.")
        return c.ai_provider, key or ""

    def _import(self, *, text: str | None, pdf: bytes | None) -> dict[str, Any]:
        from .. import ai
        from ..resume import profile as prof

        provider, key = self._ai()
        got = ai.import_profile(resume_text=text, resume_pdf=pdf, provider=provider, api_key=key,
                                model=self.ctrl.config.ai_model)
        _require(got, "The AI couldn't read that résumé. Try again, or fill in the fields yourself.", 502)
        return {"profile": prof.normalize(got)}

    def profile_import(self, data: bytes) -> dict[str, Any]:
        """Read a résumé PDF you pick into the profile fields (not saved: you check them first)."""
        _require(data[:5] == b"%PDF-", "That doesn't look like a PDF file.")
        _require(len(data) <= MAX_RESUME, "The PDF is larger than 10 MB.")
        return self._import(text=None, pdf=data)

    def profile_import_saved(self, _: dict[str, Any]) -> dict[str, Any]:
        """Same, from the résumé you already uploaded (or its text)."""
        self._ai()   # say "add an AI key" before anything else
        c = self.ctrl.config
        txt = self.ctrl.resolve(c.resume_text_path) if c.resume_text_path else None
        if txt and txt.exists() and txt.read_text("utf-8").strip():
            return self._import(text=txt.read_text("utf-8"), pdf=None)
        pdf = self.ctrl.resolve(c.resume_pdf_path or "resume.pdf")
        _require(pdf.exists(), "Upload a résumé PDF first.")
        return self._import(text=None, pdf=pdf.read_bytes())

    def profile_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """Make the résumé now (tailored to a pasted job post, if given) for you to look at."""
        from ..resume import builder as rb
        from ..resume import profile as prof
        from ..resume.render import ResumeError

        st = self.ctrl.service.state
        p = body.get("profile") if isinstance(body.get("profile"), dict) else st.get_profile()
        try:
            prof.validate(p)
        except prof.ProfileError as e:
            raise ApiError(str(e)) from e
        posting = str(body.get("posting") or "")[:20000]
        b = rb.make_builder(self.ctrl.config, self.ctrl.base_dir, p)
        if not posting.strip():
            b.tailor = None    # a general résumé is just your profile, laid out; no AI call
        try:
            path = b.build(posting, key="preview")
        except ResumeError as e:
            raise ApiError(str(e)) from e
        info = b.last
        return {"file": b.relative(path), "name": path.name, "tailored": bool(b.tailor and posting.strip()),
                "size": path.stat().st_size, "pages": info.pages, "paper": info.paper,
                "max_pages": info.max_pages, "too_long": info.too_long, "skills": info.skills.to_dict()}

    def resume_upload(self, data: bytes) -> dict[str, Any]:
        _require(data[:5] == b"%PDF-", "That doesn't look like a PDF file.")
        _require(len(data) <= MAX_RESUME, "The PDF is larger than 10 MB.")
        c = self.ctrl.config
        path = self.ctrl.resolve(c.resume_pdf_path or "resume.pdf")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".upload")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        log.info("résumé uploaded (%d KB)", len(data) // 1024)
        return build_state(self.ctrl)

    def resume_delete(self, _: Any) -> dict[str, Any]:
        _require(self.ctrl.config.apply_mode != "auto", "Turn auto-apply off before removing your résumé.")
        path = self.ctrl.resolve(self.ctrl.config.resume_pdf_path or "resume.pdf")
        if path.exists():
            path.unlink()
        return build_state(self.ctrl)

    def resume_text(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text") or "")
        _require(len(text) <= 50_000, "That résumé text is too long (50,000 characters max).")
        c = self.ctrl.config
        path = self.ctrl.resolve(c.resume_text_path or "resume.txt")
        if text.strip():
            path.write_text(text, encoding="utf-8")
            if not c.resume_text_path:
                self._save({"resume_text_path": "resume.txt"})
        else:
            if path.exists():
                path.unlink()
            if c.resume_text_path:
                self._save({"resume_text_path": None})
        return build_state(self.ctrl)

    # --------------------------------------------------- telegram account
    def _account(self) -> dict[str, str]:
        c = self.ctrl.config
        _require(c.telegram_api_id and c.telegram_api_hash,
                 "Save your API ID and API hash from my.telegram.org first.")
        return {"api_id": str(c.telegram_api_id), "api_hash": str(c.telegram_api_hash),
                "session_path": str(self.ctrl.resolve(c.telegram_session_path))}

    def _login(self, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            out = fn()
        except LoginError as e:
            raise ApiError(str(e)) from e
        if out.get("stage") == "done":
            log.info("Telegram account logged in%s", f" as {out['username']}" if out.get("username") else "")
        return out

    def tg_start(self, body: dict[str, Any]) -> dict[str, Any]:
        acct = self._account()
        phone = str(body.get("phone") or self.ctrl.config.telegram_phone or "").replace(" ", "")
        _require(phone.startswith("+") and phone[1:].isdigit(),
                 "Enter your phone number with the country code, e.g. +60123456789.")
        if phone != (self.ctrl.config.telegram_phone or ""):
            self._save({"telegram_phone": phone})
        return self._login(lambda: self.ctrl.login.start(phone=phone, **acct))

    def tg_code(self, body: dict[str, Any]) -> dict[str, Any]:
        acct = self._account()
        code = str(body.get("code") or "").strip()
        _require(code, "Enter the code Telegram sent you.")
        return self._login(lambda: self.ctrl.login.code(code, acct["session_path"]))

    def tg_password(self, body: dict[str, Any]) -> dict[str, Any]:
        acct = self._account()
        pw = str(body.get("password") or "")
        _require(pw, "Enter your two-step verification password.")
        return self._login(lambda: self.ctrl.login.password(pw, acct["session_path"]))

    def tg_check(self, _: Any) -> dict[str, Any]:
        acct = self._account()
        try:
            return TelegramLogin.check(**acct)
        except LoginError as e:
            raise ApiError(str(e)) from e

    def tg_logout(self, _: Any) -> dict[str, Any]:
        acct = self._account()
        try:
            TelegramLogin.logout(**acct)
        except LoginError as e:
            raise ApiError(str(e), 409) from e
        log.info("Telegram account logged out; session file deleted")
        return build_state(self.ctrl)


# ------------------------------------------------------------- the server
class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, ctrl: Controller, token: str, *, local_only: bool):
        self.ctrl = ctrl
        self.api = Api(ctrl)
        self.routes = self.api.routes()
        self.token = token
        self.local_only = local_only
        super().__init__(addr, Handler)

    def allowed_hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


class Handler(http.server.BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = f"JobRadar/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        log.debug("http: " + fmt, *args)

    # ------------------------------------------------------------- guards
    def _host_ok(self) -> bool:
        if not self.server.local_only:
            return True
        return (self.headers.get("Host") or "") in self.server.allowed_hosts()

    def _authed(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        got = cookie.get(COOKIE)
        return bool(got and secrets.compare_digest(got.value, self.server.token))

    def _same_origin(self) -> bool:
        if self.headers.get("X-Requested-With") != "jobradar":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urllib.parse.urlparse(origin).netloc
        return host == (self.headers.get("Host") or "")

    # ------------------------------------------------------------ helpers
    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError("request too large", 413)
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------ methods
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._error(403, "Open the dashboard at http://127.0.0.1 (the link Job Radar printed).")
        url = urllib.parse.urlparse(self.path)
        if method == "GET" and url.path == "/":
            return self._index(url)
        if method == "GET" and url.path == "/favicon.svg":
            return self._send(200, _FAVICON, "image/svg+xml")
        if not url.path.startswith("/api/"):
            return self._error(404, "not found")
        if not self._authed():
            return self._error(403, "Session expired — reopen the link Job Radar printed.")
        if method != "GET" and not self._same_origin():
            return self._error(403, "cross-site request refused")
        if method == "GET" and url.path == "/api/backup":
            from ..backup import make_backup

            q = urllib.parse.parse_qs(url.query)
            login = (q.get("login") or ["0"])[0] == "1"
            ctrl = self.server.ctrl
            data = make_backup(ctrl.config_path, include_login=login, state=ctrl.service.state)
            name = time.strftime("jobradar-backup-%Y%m%d-%H%M.zip")
            return self._send(200, data, "application/zip",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if method == "GET" and url.path == "/api/file":
            return self._resume_file(urllib.parse.parse_qs(url.query).get("path", [""])[0])
        if method == "GET" and url.path == "/api/logs":
            q = urllib.parse.parse_qs(url.query)
            after = int((q.get("after") or ["0"])[0] or 0)
            return self._json(200, {"ok": True, "lines": self.server.ctrl.logs.since(after)})
        handler = self.server.routes.get((method, url.path))
        if handler is None:
            return self._error(404, "unknown API call")
        try:
            if method == "PUT":
                from ..backup import MAX_TOTAL

                arg: Any = self._body(MAX_TOTAL if url.path == "/api/restore" else MAX_RESUME + 1)
            elif method == "GET":
                arg = None
            else:
                raw = self._body(MAX_JSON)
                arg = json.loads(raw) if raw else {}
                if not isinstance(arg, dict):
                    raise ApiError("expected a JSON object")
            result = handler(arg)
            self._json(200, {"ok": True, **(result or {})})
        except ApiError as e:
            self._error(e.status, str(e))
        except json.JSONDecodeError:
            self._error(400, "invalid JSON")
        except Exception as e:  # noqa: BLE001
            log.exception("dashboard API %s %s failed", method, url.path)
            self._error(500, redact(f"{type(e).__name__}: {e}"))

    def _resume_file(self, rel: str) -> None:
        """A generated résumé, for viewing (only files under jobradar-data/resumes)."""
        root = (self.server.ctrl.base_dir / "resumes").resolve()
        try:
            path = (self.server.ctrl.base_dir / rel).resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            return self._error(404, "not found")
        if path.suffix.lower() != ".pdf" or not path.is_file():
            return self._error(404, "That résumé file is no longer there.")
        name = urllib.parse.quote(path.name)
        return self._send(200, path.read_bytes(), "application/pdf",
                          {"Content-Disposition": f"inline; filename=\"resume.pdf\"; filename*=UTF-8''{name}"})

    def _index(self, url) -> None:
        q = urllib.parse.parse_qs(url.query)
        given = (q.get("t") or [""])[0]
        if given and secrets.compare_digest(given, self.server.token):
            cookie = (f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={60 * 60 * 24 * 365}")
            # Redirect so the token doesn't linger in the address bar/history.
            return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self._authed():
            return self._send(403, _LOCKED_PAGE, "text/html; charset=utf-8")
        csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")
        self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})


_FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="15" '
            b'fill="#2563eb"/><circle cx="16" cy="16" r="9" fill="none" stroke="#fff" stroke-width="2.5"/>'
            b'<circle cx="16" cy="16" r="3" fill="#fff"/></svg>')

_LOCKED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Job Radar</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:15vh auto;padding:0 1rem;color:#1f2937}
code{background:#f3f4f6;padding:.1rem .3rem;border-radius:4px}</style></head><body>
<h1>Job Radar</h1><p>This page is locked to protect your tokens.</p>
<p>Open the link printed in the Job Radar window (it starts with
<code>http://127.0.0.1:</code> and ends with <code>?t=…</code>), or restart Job Radar
and it will open the dashboard for you.</p></body></html>""".encode("utf-8")


# ------------------------------------------------------------- entry point
def _load_token(base_dir: Path) -> str:
    path = base_dir / "jobradar-web.token"
    try:
        tok = path.read_text("utf-8").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return tok


def make_server(config_path: Path, *, host: str | None = None, port: int = 8765,
                logs: LogBuffer | None = None) -> DashboardServer:
    """``host`` None follows the "Allow other devices" setting; a host given on
    the command line wins over it."""
    ctrl = Controller(config_path, logs)
    token = _load_token(ctrl.base_dir)
    forced = host is not None
    if host is None:
        host = "0.0.0.0" if ctrl.config.web_allow_network else "127.0.0.1"
    local = host in ("127.0.0.1", "localhost", "::1")
    last_err: OSError | None = None
    for p in ([port] + list(range(port + 1, port + 11)) if port else [0]):
        try:
            server = DashboardServer((host, p), ctrl, token, local_only=local)
        except OSError as e:  # port in use: try the next one
            last_err = e
            continue
        ctrl.network = {"listening_all": not local, "forced": forced,
                        "port": server.server_address[1], "token": token}
        return server
    raise OSError(f"no free port near {port}: {last_err}")


def main(config_path: str, *, host: str | None = None, port: int = 8765,
         open_browser: bool = True) -> int:
    logs = LogBuffer()
    logging.getLogger().addHandler(logs)
    path = Path(config_path)
    server = make_server(path, host=host, port=port, logs=logs)
    ctrl = server.ctrl
    port_ = server.server_address[1]
    url = f"http://127.0.0.1:{port_}/?t={server.token}"
    print("\n  Job Radar dashboard:  " + url, flush=True)
    print("  Keep this window open — closing it stops Job Radar.\n", flush=True)
    if not server.local_only:
        from .netinfo import addresses

        for a in addresses():
            where = "through Tailscale (from anywhere)" if a["kind"] == "tailscale" else "on your network"
            print(f"  From another device {where}:  http://{a['ip']}:{port_}/?t={server.token}", flush=True)
        print("\n  Other devices can reach the dashboard. The link above logs in whoever opens it:\n"
              "  keep it private, and only allow this on networks you trust (home Wi-Fi, Tailscale).\n")
    if not path.exists():
        log.info("no config file yet at %s — it will be created when you save settings", path)
    ctrl.start()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        ctrl.stop()
        server.server_close()
    return 0
