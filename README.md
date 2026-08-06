# Social Job Radar — Telegram edition

A tiny self-hosted bot that scrapes job postings from social platforms, keeps
the ones matching your keywords and remote preference, and pushes them to you
on **Telegram**. No database, no web dashboard, no paid services — pure Python
3.11 standard library and a single small JSON state file.

> This is a simplified build. It scrapes and sends via Telegram only. The
> original database + web-dashboard + review-queue version is preserved in git
> history (branch `claude/requirement-implementation-22epkg`, commit `43c81ee`)
> if you ever want it back.

## How it works

```
 sources ──▶ collect new items ──▶ classify (is this a job?) ──▶ extract fields
 (Tier A/B)   since last run          recall-first                (title/remote/salary)
                                                                        │
                                                                        ▼
                          send one batched          drop already-sent   keep those matching
   Telegram  ◀───────────  digest per run  ◀──────  (JSON state)  ◀───  keyword + remote filter
                                                          ▲
                                                   merge duplicates
                                                   across sources
```

There is exactly one piece of persistent state: a JSON file (default
`jobradar-state.json`) that remembers which postings were already sent — so the
bot never sends you the same job twice — plus a per-source last-seen timestamp
and a failure counter. It's written atomically, so a crash or a closed laptop
mid-write can't corrupt it.

**Delivery is safe against outages.** A posting is only recorded as "sent" after
the Telegram message actually goes out, and a source's watermark only advances
once its postings are delivered. If Telegram is down (or you haven't configured
a token yet), nothing is marked sent — the postings are simply re-detected and
sent on the next run.

## Quick start

Requires Python 3.11+. Nothing to install.

```bash
# 1. Configure — just fill in the two Telegram lines; the rest has defaults
cp config.example.toml config.toml      # edit telegram_bot_token + telegram_chat_id

# 2. Check the wiring
python3 -m jobradar.cli --config config.toml test-telegram

# 3. Run one cycle now…
python3 -m jobradar.cli --config config.toml run

# 4. …or run forever at the configured interval
python3 -m jobradar.cli --config config.toml serve
```

Everything lives in `config.toml` — including your `telegram_bot_token` and
`telegram_chat_id`. Create the bot with **@BotFather** for the token; to find
your chat id, message the bot once and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.

`config.toml` is git-ignored so your token is never committed — keep the file
private. If you'd rather not keep secrets in a file, leave them blank and set
environment variables instead (they override the file):

```bash
export JOBRADAR_TELEGRAM_BOT_TOKEN=123456:ABC...
export JOBRADAR_TELEGRAM_CHAT_ID=987654321
```

## Commands

| Command | Does |
|---|---|
| `run` | One collection cycle → one Telegram digest |
| `serve` | Loop forever, running every `run_interval_hours` |
| `sources` | List configured sources and their state |
| `test-telegram` | Send a test message to the configured chat |

## Sources

**You don't have to configure any.** If `config.toml` has no `[[sources]]`
blocks, Job Radar uses three built-in sources that need no extra keys — Bluesky,
Hacker News ("Who Is Hiring"), and WeWorkRemotely. Add a `[[sources]]` block only
if you want to monitor something specific:

| Type | Tier | Config keys | Notes |
|---|---|---|---|
| `bluesky` | A | `query` | Public AT Protocol API, no key |
| `hn` | A | `query`, `classifier_prior` | Reads the monthly "Who Is Hiring" thread |
| `rss` | A | `url` | Any RSS/Atom feed |
| `reddit` | A | `subreddit`, `listing` | Needs `JOBRADAR_REDDIT_CLIENT_ID` / `_SECRET` |
| `telegram` | A | `channel` | Bot must be in the channel |
| `scrape` | B | `url`, `block_pattern` | Best-effort HTML; expected to break |

Tier A is expected to work. Tier B is best-effort: it's fetched politely (honest
`User-Agent`, rate-limited, respects `robots.txt` / HTTP 429), and if it breaks
three runs in a row it auto-disables itself and pings you on Telegram. Adding a
new source is one class implementing the `Collector` interface.

A transient HTTP error (including a one-off `403`) is treated as a retryable
failure, not a permanent block — a single blip won't kill a source. If a source
does get disabled after repeated failures, re-enable it by deleting its entry
under `"sources"` in the state file (or just delete the state file to reset
everything; you'll only risk re-sending recent postings once).

The **HN** source specifically reads the current monthly *"Ask HN: Who is
hiring?"* thread and returns its top-level comments — the real job posts — so
titles and locations come out clean and you don't get discussion comments or
job-seeker posts.

## Optional AI extraction

Off by default. With `use_ai = true` and an Anthropic API key in the
environment, each posting *about to be sent* is enriched by Claude into cleaner
fields plus separate **岗位职责 / 岗位要求** (responsibilities / requirements)
lists — the parts rule-based parsing can't reliably split out of free-form text.

```toml
use_ai = true
ai_model = "claude-opus-5"     # or "claude-haiku-4-5" to cut cost
```
```bash
export JOBRADAR_ANTHROPIC_API_KEY=sk-ant-...
```

It costs money per posting, so the API is called only for postings that already
passed the filter and dedup (at most once each), and any failure — no key,
network error, refusal — silently falls back to the rule-based fields. The core
app still needs no third-party packages; the AI call goes over stdlib `urllib`.

## Build a standalone executable (.exe)

You can package Job Radar as a single double-clickable file so it runs without a
Python install.

```bash
python build.py          # produces dist/jobradar (or dist/jobradar.exe on Windows)
```

PyInstaller doesn't cross-compile — run `build.py` **on Windows** to get a
`.exe`. If you don't have a Windows machine, the included GitHub Actions
workflow (`.github/workflows/build-exe.yml`) builds it on a `windows-latest`
runner and uploads `jobradar-windows` (the `.exe` + a `config.toml`) as a
downloadable artifact on every push, or from the Actions tab via "Run workflow".

Put a `config.toml` next to the executable (copy `config.example.toml`, fill in
your `telegram_bot_token` and `telegram_chat_id`) and run it — a double-clicked
`.exe` with no arguments starts the daemon (`serve`), and its state file is
written next to the executable. The `config.toml` beside the `.exe` holds
everything; keep it private since it contains your token.

## Filtering

Three stages, in order; a posting must pass all the ones you've configured:

1. **Keyword** — case-insensitive, variant-aware (`fullstack` also matches
   `full-stack` / `full stack`). At least one keyword must match. An empty
   `keywords` list means "send everything".
2. **Remote** — with `remote_only = true`, on-site/hybrid roles are dropped;
   explicit-remote and unknown-arrangement roles pass.
3. **Region** — set `regions` to the countries/cities/blocs you care about
   (e.g. `["Hong Kong", "Malaysia", "Worldwide"]`). A posting then passes only
   if it hires **worldwide/anywhere** OR its **location names one of your
   regions**. So if you live in Malaysia and set `regions = ["Hong Kong"]`, you
   get Hong Kong-remote roles and hire-from-anywhere roles, but not US-only
   ones. Leave `regions = []` to skip this stage.

## Delivery

Each matching posting is sent as its **own Telegram message** (not a batched
digest), oldest first, with a small gap between messages to stay under
Telegram's flood limit. A posting is recorded as "sent" the moment its message
goes out, so nothing is ever sent twice and an outage only retries what didn't
make it.

## Project layout

```
jobradar/
  config.py          keywords, remote_only, telegram, sources, interval
  state.py           the JSON state file (already-sent memory)
  classifier.py      recall-first "is this a job posting?"
  extraction.py      title / remote / salary / contact / apply-url
  filters.py         keyword + remote
  dedup.py           near-match duplicate detection
  pipeline.py        the whole run: collect → filter → dedup → send
  service.py         wiring + the serve loop
  cli.py             entry point
  collectors/        one class per source type (Tier A + Tier B)
  delivery/          digest formatting + Telegram sender
```

## Notes

- Non-commercial use only (a condition of Reddit's free tier).
- Tier B scraping conflicts with some sites' terms; the accepted consequence is
  losing access to those sites, which degrades gracefully — Tier A keeps working.
- The `contact` field can be personal data; it's kept only in the posting text
  and never written to logs.
