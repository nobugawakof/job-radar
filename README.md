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
# 1. Configure
cp config.example.toml config.toml      # edit keywords + [[sources]]

# 2. Set your Telegram secrets (from @BotFather, and your chat id)
export JOBRADAR_TELEGRAM_BOT_TOKEN=123456:ABC...
export JOBRADAR_TELEGRAM_CHAT_ID=987654321

# 3. Check the wiring
python3 -m jobradar.cli --config config.toml test-telegram

# 4. Run one cycle now…
python3 -m jobradar.cli --config config.toml run

# 5. …or run forever at the configured interval
python3 -m jobradar.cli --config config.toml serve
```

Getting your chat id: message your bot once, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.

## Commands

| Command | Does |
|---|---|
| `run` | One collection cycle → one Telegram digest |
| `serve` | Loop forever, running every `run_interval_hours` |
| `sources` | List configured sources and their state |
| `test-telegram` | Send a test message to the configured chat |

## Sources

Enable/disable and configure sources as data in `config.toml` — no code changes.

| Type | Tier | Config keys | Notes |
|---|---|---|---|
| `bluesky` | A | `query` | Public AT Protocol API, no key |
| `hn` | A | `query`, `classifier_prior` | Algolia "Who Is Hiring" search |
| `rss` | A | `url` | Any RSS/Atom feed |
| `reddit` | A | `subreddit`, `listing` | Needs a Reddit OAuth app |
| `telegram` | A | `channel` | Bot must be in the channel |
| `scrape` | B | `url`, `block_pattern` | Best-effort HTML; expected to break |

Tier A is expected to work. Tier B is best-effort: it's fetched politely (honest
`User-Agent`, rate-limited, respects `robots.txt` / HTTP 429), and if it breaks
three runs in a row it auto-disables itself and pings you on Telegram. Adding a
new source is one class implementing the `Collector` interface.

## Filtering

1. **Keyword** — case-insensitive, variant-aware (`fullstack` also matches
   `full-stack` / `full stack`). At least one keyword must match. An empty
   `keywords` list means "send everything".
2. **Remote** — with `remote_only = true`, on-site/hybrid roles are dropped;
   explicit-remote and unknown-arrangement roles pass.

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
