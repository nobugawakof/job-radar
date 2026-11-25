# Social Job Radar — Telegram edition

A tiny self-hosted bot that scrapes job postings from social platforms, keeps
the ones matching your keywords and remote preference, and pushes them to you
on **Telegram**. It can also apply for you, and you set it up from a local web
dashboard. No paid services and nothing to install: pure Python 3.11 standard
library, with a single-file **SQLite** database (`jobradar.db`).

> An earlier, larger implementation (with a review queue) is preserved in git
> history (branch `claude/requirement-implementation-22epkg`, commit `43c81ee`).

## How it works

```
 sources ──▶ collect new items ──▶ classify (is this a job?) ──▶ extract fields
 (Tier A/B)   since last run          recall-first                (title/remote/salary)
                                                                        │
                                                                        ▼
                          send one message          drop already-sent   keep those matching
   Telegram  ◀───────────   per posting     ◀──────  (jobradar.db) ◀───  keyword/remote/region/salary
                                                          ▲
                                                   merge duplicates
                                                   across sources
```

Everything Job Radar remembers lives in one **SQLite** database file,
`jobradar.db`, next to your config. SQLite is built into Python, so there's no
database server to install or run. The file holds:

| Table | What it remembers |
|---|---|
| `sent` | Fingerprints of every job already sent, so nothing is sent twice |
| `jobs` | Each delivered job: title, link, source, salary, location and the full original text |
| `signatures` | Similarity signatures of sent jobs, to recognise reworded reposts |
| `sources` | Each source's last successful check, failure count, and whether it's switched off |
| `applications`, `apply_queue`, `contacts`, `apply_paused` | Auto-apply history, waiting jobs, who was contacted, and paused channels |
| `meta` | The last run time, plus the schema version in SQLite's `user_version` |

Every change is committed as it happens, in a transaction, with SQLite's
write-ahead log (WAL). A crash or a closed laptop can't leave the file
half-written, and the dashboard and a command-line run can use it at the same
time. Old tables are kept to size automatically (`max_sent_remembered`, default
5,000 per table, and `repost_window_days` for signatures). You can open the file
with any SQLite tool, for example `sqlite3 jobradar.db "select sent_at, title from jobs order by sent_at desc limit 10"`.

**Upgrading from an older version:** the first time it starts, Job Radar imports
the old `jobradar-state.json` into `jobradar.db` and renames the JSON to
`jobradar-state.json.migrated`. Nothing is re-sent.

**Delivery is safe against outages.** A posting is only recorded as "sent" after
the Telegram message actually goes out, and a source's watermark only advances
once its postings are delivered. If Telegram is down (or you haven't configured
a token yet), nothing is marked sent — the postings are simply re-detected and
sent on the next run.

## Quick start — the dashboard (recommended)

Everything is set up from a page in your browser: no config file to edit.

- **Windows:** download `jobradar-windows` from the repo's **Actions** tab
  (the *build-exe* workflow), unzip it, and double-click `jobradar.exe`.
- **Python 3.11+** (any OS):
  ```bash
  python3 -m jobradar.cli web
  ```

Your browser opens the dashboard, which walks you through three steps:

1. **Telegram bot.** Create a bot with @BotFather, paste its token, press
   **Start** in your bot, then click **Find my chat**. No need to look up your
   chat id by hand.
2. **Job search.** Tick the sites to search and type your keywords.
3. **Switch on automatic checks** on the Overview page.

Optional tabs: **AI** (paste a free Gemini key, then click **Test**) and
**Auto-apply** (upload your résumé, log in to your Telegram account with a
code, set up email, and preview drafts). **Applications** and **Activity** show
what happened and why.

Keep the window open: closing it stops Job Radar. The dashboard only accepts
connections from your own computer. Opening it needs the link the program
prints, which logs your browser in with a private cookie, and secrets are never
shown back in full. Settings are saved to `config.toml`; your original file is
kept as `config.toml.bak` the first time you save.

## Quick start — command line

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
| `web [--port N] [--no-browser]` | Open the dashboard (set up, run and watch everything in your browser) |
| `run` | One collection cycle → one Telegram digest |
| `serve` | Loop forever, running every `run_interval_hours` |
| `sources` | List configured sources and their state |
| `test-telegram` | Send a test message to the configured chat |
| `telegram-login` | Log **your own** Telegram account in, once (for auto-apply) |
| `test-apply [--posting FILE] [--send]` | Draft an application for a sample (or real) posting; `--send` delivers it to yourself |
| `applications` | List recent auto-apply results, the queue, and any paused channel |

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

**Telegram job channels:** add `t.me/<channel>` (or `@channel`) to either
list. With your own Telegram account logged in, the same login auto-apply
uses (dashboard → Auto-apply → Your Telegram account), Job Radar reads any
*public* channel's recent posts without joining it. Without that login, it can
only read channels your bot has been added to. Your bot's messages are read
by a single background "inbox" that remembers its place, so nothing is read
twice or missed when many arrive.

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
| `telegram` | A | `channel` | Or just list `t.me/<channel>` / `@channel` in `en`/`cn`. Reads any public channel through your logged-in Telegram account (see Auto-apply); without it, only channels your bot was added to |
| `scrape` | B | `url`, `block_pattern` | Best-effort HTML; expected to break |

### Chinese (and other non-English) jobs

The classifier, remote detection, and keywords understand **Chinese** as well as
English — a post like "招聘后端工程师，全球远程" is detected, and the keyword
`backend` also matches `后端` (likewise `前端`, `全栈`, `远程`, `算法`…). Just add
Chinese sites to the `cn` array (V2EX, Ruby China ship as sensible defaults).
Duplicate detection reads Chinese too: the same job cross-posted to two V2EX
nodes, or reposted with a small edit, arrives once. Two different jobs that
merely share a tech stack ("Golang", "AI") are kept apart.

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
does get disabled after repeated failures, turn it back on from the dashboard
(**Job search → Source status**). To reset everything, delete `jobradar.db`;
you'll only risk re-sending recent postings once.

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
ai_model          = "claude-haiku-5-5"   # cheaper, or "claude-opus-5-5"
anthropic_api_key = "sk-ant-..."
```

The API is called only for postings that already passed the filter and dedup (at
most once each), and any failure — no key, network error, refusal, rate limit —
silently falls back to the rule-based fields, so it never breaks a run. Gemini's
free tier is rate-limited but comfortably handles a personal bot's volume. The
core app still needs no third-party packages; both AI calls go over stdlib
`urllib`.

## Auto-apply (optional)

Off by default. When on, the bot doesn't just tell you about a job: it sends
the hiring contact a short application message with your **résumé PDF**
attached. Postings that name a **Telegram handle** get the message from **your
own Telegram account**. Postings that only give an **email** get an email.

```
delivered posting ─▶ names a contact? ─▶ passes apply gates? ─▶ AI fit score ≥ threshold?
   ─▶ draft (in the post's language) ─▶ dry_run: show it to you   |   auto: send + résumé
   ─▶ record (never the same posting twice, never the same person twice in 30 days)
```

### Set it up

1. **Résumé.** Put `resume.pdf` next to `config.toml`. Optionally also a
   plain-text copy (`resume_text_path`). The AI reads the text if you give
   one, otherwise it reads the PDF directly.
2. **AI (recommended).** Set `use_ai = true` and a Gemini or Claude key (see
   above). For each posting the AI scores how well your résumé fits (0–100) and
   writes a short, specific message. It uses only facts from your résumé, in
   the posting's language. Postings below `apply_min_score` are skipped.
   Without AI, `apply_keywords` is required and every application uses your
   fixed `apply_template`.
3. **Telegram account** (for Telegram contacts). A bot can't message people who
   never started it, so this logs in as *you*:
   ```bash
   pip install telethon
   ```
   Get `telegram_api_id` / `telegram_api_hash` at <https://my.telegram.org> →
   *API development tools*, put them in `config.toml`, then run once:
   ```bash
   python3 -m jobradar.cli --config config.toml telegram-login
   ```
   It asks for the code Telegram sends you (and your 2FA password, if set) and
   saves `jobradar-user.session`. **Keep that file private: it gives full access
   to your Telegram account.** It's git-ignored.
4. **Email** (for email-only postings). Fill in the `smtp_*` settings. For
   Gmail, use `smtp.gmail.com:587` with an
   [App Password](https://myaccount.google.com/apppasswords).
5. **Check the wiring:**
   ```bash
   python3 -m jobradar.cli --config config.toml test-apply            # prints the draft
   python3 -m jobradar.cli --config config.toml test-apply --send     # sends it to YOU
   ```
   `--send` delivers it to your Telegram *Saved Messages* and your own inbox,
   so you see exactly what a hiring manager would get. Use `--posting job.txt`
   to draft against a real posting.

### Dry run first, then go live

```toml
apply_mode = "dry_run"
```

In dry run, everything happens (gates, AI scoring, drafting, the daily cap)
except contacting anyone. Each draft shows up in your bot chat as
**📝 DRY RUN — would apply (fit 82/100)** with the recipient, the reason, and the
full text. Run it for a few days and tune `apply_min_score`, `apply_keywords`
and `apply_exclude_keywords` until you'd be happy to send every draft. Then:

```toml
apply_mode = "auto"
```

Each real application is followed by **✅ Applied via Telegram @…** in your bot
chat. `applications` lists everything that was sent, skipped (low fit /
cooldown) or failed, and why. Postings you saw in dry run aren't re-sent when
you switch, but going live doesn't reuse dry-run cooldowns, so new postings
from the same contacts are fair game.

### Who gets messaged, and the safety rails

- **Only an explicit application contact.** That means a `t.me/<user>` link,
  `Telegram: @user` / `TG @user` / `电报 @user`, or, for posts that came from a
  Telegram channel, `Contact/DM/联系 @user`. A bare `@name` on X, Bluesky or
  Reddit is that site's handle and is ignored. Bots, the channel itself, and
  no-reply addresses are never contacted. The recipient always comes from these
  rules, never from the AI.
- **Once per posting, once per person.** No follow-ups, and no second message
  to the same contact within `apply_contact_cooldown_days` (30 by default),
  even for a different job.
- **Daily cap and pacing.** At most `apply_daily_cap` (10) per rolling 24 hours,
  with a random 45–120 s gap between sends. Anything over the cap waits in a
  queue for the next run and is dropped once it's older than
  `apply_max_age_days`.
- **Back off on the first warning.** If Telegram reports flood limits
  (`PEER_FLOOD` / `FLOOD_WAIT`), Telegram applying pauses (24 h for
  `PEER_FLOOD`), you get an alert, and postings that also list an email go by
  email instead. Check **@SpamBot** in Telegram if you see this.
- **Crash-safe.** Each application is committed to the database the moment
  it's sent, so a crash or a closed laptop can't make it apply twice.

> **Know the risk.** Telegram restricts accounts that message many strangers,
> and new accounts are flagged fastest. Keep the cap low, use an account with
> some history, and don't lower the cooldown. Applications are sent in your
> name, so read your dry-run drafts before switching to `auto`.

Coverage: most HN, WeWorkRemotely and Reddit posts ask you to apply through a
web form, and those aren't automated. Auto-apply only covers posts that give a
Telegram contact or an email address.

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

Double-click the `.exe` and the dashboard opens in your browser, where you
set everything up. Settings, state and your résumé are stored next to the
executable. The `config.toml` beside it holds your tokens, so keep that folder
private. To run it headless instead (for example on a server), use
`jobradar.exe serve` with a hand-written `config.toml`.

## Filtering

First, the classifier drops anything that isn't an actual employer job post —
job-seeker / "求职" posts, "求内推" referral-begging, coffee-chat and
career-advice threads, laid-off venting, freelancer-for-hire ads — so a post like
"「求助帖」5 年前端又被裁…" never reaches you. What survives then passes through
these stages, in order; a posting must pass all the ones you've configured:

0. **Blocklist** — `exclude_keywords` drops whole role categories you never
   want (e.g. `["marketing", "growth", "sales", "市场推广", "用户增长"]`), even
   when the post matches a wanted keyword. Use it for "engineering only" so a
   marketing post that says "data-driven" doesn't sneak in via the `data`
   keyword. Empty = nothing blocked.
1. **Keyword** — case-insensitive, variant-aware (`fullstack` also matches
   `full-stack` / `full stack`, `backend` also matches `后端`). At least one
   keyword must match. An empty `keywords` list means "send everything".
2. **Remote** — with `remote_only = true`, on-site/hybrid roles are dropped;
   explicit-remote and unknown-arrangement roles pass.
3. **Region** — set `regions` to the countries, cities or groupings you can
   work from (e.g. `["Malaysia", "APAC"]`, `["Germany"]`, `["中国"]`). A job
   passes if it hires **worldwide** or its hiring area **overlaps** yours.
   Places are compared as sets of countries, so `Germany` matches "EU only",
   "Europe", "EMEA" and "Berlin". Likewise `Malaysia` matches "APAC",
   "Southeast Asia" and "Kuala Lumpur", and `中国` matches 国内, 北京 and
   "Shanghai". A job limited to somewhere else ("US only", "Remote (LATAM)") is
   dropped. The built-in list of countries, major cities and groupings (EU,
   EEA, Europe, EMEA, DACH, Nordics, CEE, APAC, Asia, Southeast Asia, Greater
   China, ANZ, North America, LATAM, Americas…) lives in `jobradar/places.py`.
   Anything it doesn't know, like "Bay Area North", is matched as plain text.
   A city you type also matches jobs open to its whole country. Leave
   `regions = []` to skip this stage.
   **Jobs that don't say where they hire** aren't silently dropped. They're
   sent with "📍 Location: not stated", so you can check for yourself. Set
   `send_unknown_location = false` (or untick it in the dashboard) to drop them
   instead. On-site and hybrid jobs whose place isn't recognised are dropped,
   since an office is somewhere specific.
   A job counts as **hire-from-anywhere** only when the post says so about
   the job: "Remote (Worldwide)", "work from anywhere in the world",
   "Location: Global", 全球远程, 工作地点不限. Company descriptions like "a global
   fintech", "customers worldwide" or 全球领先 don't count. Neither does anything
   limited to a place: "anywhere in the US", "Remote within Canada", "US only",
   "must be based in the UK", 国内远程. On-site and hybrid jobs never count.
4. **Salary** — set `min_salary_usd` to drop jobs whose stated pay is below
   your floor, roughly converted to annual US dollars. Job Radar reads:
   - **Currencies:** $, €, £, HK$, S$, A$, C$, NT$, RM, ₱, ₹, ¥/￥, and codes
     like HKD, SGD, CNY or MYR. ¥ is yuan or yen depending on where the job is.
   - **Chinese formats:** 月薪 20-30K, ·14薪 or 16薪 (paid months per year),
     年薪 50-80万, 2万/月, 待遇 15k-25k, 元, 港币.
   - **Pay periods:** /hour, /day, "day rate", /month, p.a.

   It ignores numbers that aren't pay: funding ("raised $20M", "Series A"),
   percentages, 401k, years and team sizes. When the period can't be told
   ("Salary: $8,000"), the job isn't judged on pay rather than guessed at.
   Jobs that don't state a salary pass too, unless you set
   `require_salary = true`, which only sends jobs that state one. Leave
   `min_salary_usd = 0` and `require_salary = false` to skip this stage.
   Exchange rates are approximate (`jobradar/salary.py`).

Postings older than `max_posting_age_days` (default 15) are skipped as well.

## Duplicates and reposts

The same job is sent once, even when it's cross-posted to several sources in
the same run (one message lists every source) or **reposted later with small
edits**, like next month's HN thread, a bumped salary, or a cross-post with a new
title. To catch reposts, the bot keeps a compact similarity signature of each
job it sent for `repost_window_days` (default 60; `0` turns it off). A job that
keeps being reposted keeps being recognised, because each repost refreshes the
memory.

Two postings are never treated as duplicates when their titles name clearly
different roles, such as *Junior* vs *Senior*, *Backend* vs *Frontend*, or 初级 vs
高级, even if they share most of their text (a company's boilerplate). A
complete rewrite of the same job can't be recognised and will arrive again.

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
  state.py           the SQLite database (jobradar.db): sent jobs, sources, history
  classifier.py      recall-first "is this a job posting?"
  extraction.py      title / remote / salary / contact / apply-url
  filters.py         keyword + remote
  dedup.py           near-match duplicate detection
  pipeline.py        the whole run: collect → filter → dedup → send
  service.py         wiring + the serve loop
  cli.py             entry point
  collectors/        one class per source type (Tier A + Tier B)
  delivery/          digest formatting + Telegram sender
  apply/             auto-apply: contacts, engine (gates/caps/queue), senders
  web/               the dashboard: app.py (server + API), index.html, configfile.py
tests/               python -m unittest discover -s tests
```

## Notes

- Non-commercial use only (a condition of Reddit's free tier).
- Tier B scraping conflicts with some sites' terms; the accepted consequence is
  losing access to those sites, which degrades gracefully — Tier A keeps working.
- The `contact` field can be personal data; it's kept only in the posting text
  and never written to logs.
