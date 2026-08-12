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
                          send one message          drop already-sent   keep those matching
   Telegram  ◀───────────   per posting     ◀──────  (JSON state)  ◀───  keyword/remote/region/salary
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

There is a **single** config file — `config.toml`, copied from
`config.example.toml`. Everything lives in it: your `telegram_bot_token` and
`telegram_chat_id`, the sites to search, filters, and any API tokens. Create the
bot with **@BotFather** for the token; to find your chat id, message the bot once
and open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
`message.chat.id`.

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

**Just list the sites you want** in two arrays — `en` (global/English) and `cn`
(Chinese). The split is only for your own tidiness; both are searched together.
Each URL is turned into the right kind of source automatically:

```toml
en = [
  "news.ycombinator.com",     # HN "Who is hiring?" monthly thread
  "weworkremotely.com",
  "reddit.com/r/forhire",     # needs reddit_client_id/secret below
]
cn = [
  "v2ex.com",                 # V2EX 酷工作 + 远程
  "ruby-china.org",
]
```

Job Radar recognises common domains (Hacker News, WeWorkRemotely, V2EX, Ruby
China, Reddit `/r/<sub>`, X/Twitter, Bluesky) and maps them to the correct
collector. An unknown URL that looks like a feed (`.rss` / `.xml` / `.atom` /
`/feed`) is fetched as RSS; anything else is a best-effort scrape. If both `en`
and `cn` are empty, three built-in sources are used (Bluesky, Hacker News,
WeWorkRemotely).

All API tokens go in `config.toml` (see the token section of
`config.example.toml`) — no environment variables needed. Some sources need one:
`reddit.com` needs `reddit_client_id` / `reddit_client_secret`, and `x.com` needs
`x_bearer_token` (a **paid** X API plan). **Facebook and TikTok are not
supported**: neither offers a usable way to search public job posts.

**Advanced:** for a specific RSS feed or scrape target the URL lists don't cover,
add `[[sources]]` blocks — they're used *in addition to* `en`/`cn`:

| Type | Tier | Config keys | Notes |
|---|---|---|---|
| `bluesky` | A | `query` | Public AT Protocol API, no key |
| `hn` | A | `query`, `classifier_prior` | Reads the monthly "Who Is Hiring" thread |
| `rss` | A | `url` | Any RSS/Atom feed |
| `reddit` | A | `subreddit`, `listing` | Set `reddit_client_id` / `reddit_client_secret` in config |
| `twitter` (`x`) | A | `query` | Set `x_bearer_token` in config — **requires a paid X API plan** |
| `telegram` | A | `channel` | Bot must be in the channel |
| `scrape` | B | `url`, `block_pattern` | Best-effort HTML; expected to break |

### Chinese (and other non-English) jobs

The classifier, remote detection, and keywords understand **Chinese** as well as
English — a post like "招聘后端工程师，全球远程" is detected, and the keyword
`backend` also matches `后端` (likewise `前端`, `全栈`, `远程`, `算法`…). Just add
Chinese sites to the `cn` array (V2EX, Ruby China ship as sensible defaults).

Note: big Chinese job sites like BOSS直聘 (zhipin), 拉勾 (lagou), 猎聘 (liepin),
智联 (zhaopin), Weibo, Douban, and LinkedIn are **not scrapable** — they're
JavaScript-rendered and behind bot-blocking login walls, so no free method
reaches their listings. Listing one in `en`/`cn` just logs a skip. Sites that
publish an RSS feed (V2EX, Ruby China, …) work with no keys.

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

Off by default — **the bot works fully without it.** When on, each posting
*about to be sent* is enriched into cleaner fields plus separate
**Responsibilities / Requirements** lists — the parts rule-based parsing can't
reliably split out of free-form text. Two providers:

**Google Gemini — has a free tier** (recommended). Get a key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey):

```toml
use_ai         = true
ai_provider    = "gemini"
ai_model       = "gemini-2.5-flash"
gemini_api_key = "AIza..."
```

**Claude (Anthropic) — paid**, needs credits at console.anthropic.com:

```toml
use_ai            = true
ai_provider       = "claude"
ai_model          = "claude-haiku-4-5"   # cheaper, or "claude-opus-5"
anthropic_api_key = "sk-ant-..."
```

The API is called only for postings that already passed the filter and dedup (at
most once each), and any failure — no key, network error, refusal, rate limit —
silently falls back to the rule-based fields, so it never breaks a run. Gemini's
free tier is rate-limited but comfortably handles a personal bot's volume. The
core app still needs no third-party packages; both AI calls go over stdlib
`urllib`.

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

First, the classifier drops anything that isn't an actual employer job post —
job-seeker / "求职" posts, "求内推" referral-begging, coffee-chat and
career-advice threads, laid-off venting, freelancer-for-hire ads — so a post like
"「求助帖」5 年前端又被裁…" never reaches you. What survives then passes through
these stages, in order; a posting must pass all the ones you've configured:

1. **Keyword** — case-insensitive, variant-aware (`fullstack` also matches
   `full-stack` / `full stack`, `backend` also matches `后端`). At least one
   keyword must match. An empty `keywords` list means "send everything".
2. **Remote** — with `remote_only = true`, on-site/hybrid roles are dropped;
   explicit-remote and unknown-arrangement roles pass.
3. **Region** — set `regions` to the countries/cities/blocs you care about
   (e.g. `["Hong Kong", "Malaysia", "Worldwide"]`). A posting then passes only
   if it hires **worldwide/anywhere** OR its **location names one of your
   regions**. So if you live in Malaysia and set `regions = ["Hong Kong"]`, you
   get Hong Kong-remote roles and hire-from-anywhere roles, but not US-only
   ones. Leave `regions = []` to skip this stage.
4. **Salary** — set `min_salary_usd` to drop postings whose stated pay looks
   below your floor (roughly normalised to annual USD across currencies and
   pay periods). Postings that don't state a salary still pass, since we can't
   judge them — unless you set `require_salary = true`, which sends only postings
   that name a figure. Leave `min_salary_usd = 0` and `require_salary = false`
   to skip this stage.

Postings older than `max_posting_age_days` (default 15) are skipped as well.

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
