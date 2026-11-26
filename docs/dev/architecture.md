# Architecture

## Data flow of one check

```
Service.run (service.py)  ── takes the cross-process run lock (one check at a time)
  └─ Pipeline.run (pipeline.py)
       collectors/*         fetch each source (failures isolated per source; B1)
       classifier.py        "is this a job post?" (recall-first)
       extraction.py        title, remote, salary (salary.py), location (geo.py + places.py), contact, apply URL
       ai.py (optional)     AI extraction when use_ai
       filters.py           keywords → exclude → remote → region → salary → age
         region unclear  →  review queue (delivery/review.py; S1) instead of dropping
       dedup.py             exact hash + MinHash/LSH near-duplicates and reworded reposts (CJK aware)
       delivery/telegram.py one message per job to the bot chat (digest.py formats)
       apply/engine.py      auto-apply delivered posts that name a contact (off | dry_run | auto)
  └─ sync_conversations     read new messages in recruiter chats (apply/conversations.py)
  └─ state.record_run       run history (B9)
```

`jobradar web` (web/app.py) runs the same `Service` on a runner thread:
- a scheduled check every `run_interval_hours`, or manual with "Run now";
- a conversation sync every 300 s.

`jobradar serve` is the same without the UI.

## Modules

| Path | Role |
|---|---|
| `config.py` | `Config` dataclass: every setting and its default. Loaded from TOML plus `JOBRADAR_*` env vars |
| `datadir.py` | The `jobradar-data` folder (config, db, session, résumé, token), with migration from loose files |
| `state.py` | SQLite store (`jobradar.db`), WAL, `PRAGMA user_version` migrations, and every read and write |
| `backup.py` | One-zip backup and restore (SQLite online backup API). Replaced files go to `before-restore-<time>/` |
| `tgclient.py` | `LoopThread` (private asyncio loop, hard timeouts), `make_client`, `ACCOUNT_LOCK` |
| `redact.py` | Strips secrets from text, for logs and the dashboard |
| `collectors/` | One class per source type. `registry.py` maps a type name to its class. `telegram.py` reads public channels via the account, otherwise the bot inbox |
| `delivery/inbox.py` | The only reader of the bot's `getUpdates`. Confirms updates and persists the offset (B5) |
| `delivery/review.py` | The batched ✅/❌ review prompt in Telegram |
| `apply/contacts.py` | Finds the explicit application contact (Telegram handle or email) |
| `apply/engine.py` | `Applier`: gates, AI or template draft, daily cap, pacing, queue, flood back-off, recording, notices |
| `apply/senders.py` | `TelegramUserSender` and `EmailSender`. Both return a `SendResult` (peer id, name, message ids) |
| `apply/conversations.py` | `sync_telegram` mirrors recruiter chats; `send_reply` replies by Telegram or email |
| `apply/language.py` | `resolve(setting, text)` → zh/en; `PHRASES` + `t()` for notices; `ui()` |
| `ai.py` | Gemini and Claude over raw HTTPS (stdlib). `draft_application(..., language=)` for fit score + message |
| `web/app.py` | `http.server` dashboard: JSON API under `/api/*`, runner thread, `build_state()` |
| `web/configfile.py` | Validates and writes `config.toml` atomically. `SECTIONS`, `_CHOICES`, `SECRET_KEYS` |
| `web/tglogin.py` | Phone → code → 2FA login for the user's Telegram account, plus check and logout |
| `web/index.html` | The whole UI (vanilla JS). Tabs: overview, telegram, search, ai, apply, messages, review, history, activity |

## Database (`jobradar.db`, schema v5)

| Table | Holds |
|---|---|
| `meta` | Key/value (bot inbox offset and similar) |
| `sent` | Fingerprints of delivered posts (exact dedup) |
| `signatures` | MinHash signatures for near-duplicate and repost detection |
| `jobs` | Delivered jobs (the dashboard's recent list) |
| `sources` | Per-source health, failures and switched-off state |
| `applications` | One row per posting: mode, status, channel, contact, title, **message** (exact text sent or drafted), score, reason, error |
| `apply_queue` | Applications waiting for the daily cap |
| `contacts` | Last time each contact was messaged (cooldown) |
| `apply_paused` | Telegram flood back-off |
| `runs` | Check history |
| `bot_chats`, `bot_posts` | Bot inbox: chats seen, channel posts buffered |
| `review_queue` | Jobs with an unclear region waiting for ✅/❌ |
| `conversations` | One per representative: `(channel, contact)` unique, display_name, peer_id, last_message_at, unread |
| `messages` | Each message: direction `out` or `in`, text, subject, attachment, external_id (Telegram message id or email Message-ID, unique per conversation), job_fingerprint, job_title |

Some files are deliberately **not** in the database:
- `config.toml`: secrets, so that sharing the database never leaks a key.
- `jobradar-user.session`: Telethon's own SQLite file.
- The résumé files.

The backup zip bundles all of them; the Telegram login is included only on
request.

## Dashboard security

The dashboard is protected in these ways:
- It listens on localhost only and checks the Host header.
- The printed link `?t=<token>` sets an HttpOnly cookie (`jobradar_session`).
  The token lives in `jobradar-web.token`.
- Requests that change anything need the `X-Requested-With: jobradar` header
  and a matching Origin.
- A strict CSP applies: no inline handlers and no external scripts.
- Secrets are never sent back to the browser, only "set (hint)".
