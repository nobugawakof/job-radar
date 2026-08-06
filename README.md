# Social Job Radar — Phase 1

A self-hosted collector that watches social platforms for job postings, filters
them against each user's keywords, remote preference, and work eligibility, and
delivers the survivors through a Telegram bot and a web dashboard.

This repository implements **Phase 1** of the
[Software Requirements Specification](docs/SRS.md) — collection, extraction,
rule-based filtering, a review queue, and delivery. Phase 1 is a *collector*,
not a matcher: it answers "what job posts exist that fit my stated criteria?"
and stores enough raw text for a future Phase 2 to rank them.

## Design at a glance

* **Zero budget, zero services.** The entire Phase 1 core is pure Python
  standard library — no web framework, no third-party data store, no paid API.
  It runs on a personal laptop (SRS §2.3) and stores everything in a single
  SQLite file (DR-1/DR-2).
* **Built for an intermittent host.** The scheduler detects collection windows
  missed while the laptop was asleep and runs a bounded catch-up on startup
  (FR-4/FR-5). Every run is crash-safe: raw content is stored before parsing,
  each item is processed in its own transaction, and a killed run resumes
  cleanly (FR-6, NFR-4).
* **Source tiers are structural.** Tier A sources are expected to work; a Tier B
  scraper that breaks three runs in a row auto-disables and alerts the owner
  (SR-4) without affecting anything else (NFR-15). Disabling every Tier B source
  leaves the system fully functional on Tier A (AC-7).
* **Add a source, touch nothing else.** A new source is one class implementing
  the `Collector` interface, registered by type (NFR-13).

## Architecture

```
             ┌─────────────┐   collect (FR-2)   ┌──────────────┐
 sources ───▶│ Collectors  │───────────────────▶│  raw_items   │  stored before
 (Tier A/B)  │ reddit,bsky,│   store raw (FR-6)  │  (durable)   │  parsing (FR-6)
             │ hn,rss,tg,  │                     └──────┬───────┘
             │ scrape      │                            │
             └─────────────┘                            ▼
                                              ┌────────────────────┐
                                              │  Pipeline          │
                                              │  classify (FR-7/8) │
                                              │  extract  (FR-10-12)│
                                              │  dedup    (FR-27-29)│
                                              └─────────┬──────────┘
                                                        │ fan-out per user
                                       ┌────────────────┴───────────────┐
                                       ▼                                 ▼
                             ┌───────────────────┐            ┌───────────────────┐
                             │  Filters (4.4)    │            │  Filters (4.4)    │
                             │  user A settings  │            │  user B settings  │
                             └─────────┬─────────┘            └─────────┬─────────┘
                       pass│review│reject                 pass│review│reject
                                       ▼                                 ▼
                             ┌───────────────────────────────────────────────┐
                             │  user_postings  (per-user status & keywords)   │
                             └───────┬───────────────────────────────┬───────┘
                                     ▼                               ▼
                           ┌──────────────────┐            ┌──────────────────┐
                           │ Telegram digest  │            │  Web dashboard    │
                           │ + review queue   │            │  (localhost)      │
                           │ (batched, FR-31) │            │  FR-34/35, admin  │
                           └──────────────────┘            └──────────────────┘
```

The key modelling decision: a **posting** is collected once and stored globally,
while a **user_posting** row holds each member's own status and matched
keywords. That is what lets two members with different keyword sets get
different digests (AC-6) while a job cross-posted to two sources is still stored
and delivered once (AC-5).

### Module map

| Module | Responsibility | Key requirements |
|---|---|---|
| `jobradar/db.py` | SQLite schema, WAL, transactions, backups | DR-1/2, NFR-4/7 |
| `jobradar/repos.py` | All SQL; per-user scoping | NFR-8 |
| `jobradar/geo.py` | Country/eligibility, worldwide rule | FR-17-20 |
| `jobradar/classifier.py` | Recall-first job detection | FR-7/8 |
| `jobradar/extraction.py` | Fields, remote detection, salary | FR-10-12 |
| `jobradar/filters.py` | Three-stage filter pipeline | §4.4 |
| `jobradar/dedup.py` | Near-match deduplication | FR-27-29 |
| `jobradar/collectors/` | Source interface + Tier A/B collectors | §3, NFR-13/15 |
| `jobradar/pipeline.py` | Run orchestration, failure isolation | FR-1-12, SR-3/4 |
| `jobradar/scheduler.py` | Interval + startup catch-up + backups | FR-1/4/5, NFR-7 |
| `jobradar/delivery/` | Digest, Telegram bot, durable delivery | §4.5/4.7, IR-1-4 |
| `jobradar/web/` | Dashboard + admin (localhost) | §6.2, IR-5-8 |
| `jobradar/cli.py` | Entry point | — |

## Quick start

Requires Python 3.11+. No dependencies to install.

```bash
# 1. Configure (copy the example and edit)
cp config.example.toml config.toml

# 2. Initialise the DB, create the owner, seed sources from config
python3 -m jobradar.cli --config config.toml init

# 3. Add a member (prints their dashboard token + Telegram link code)
python3 -m jobradar.cli --config config.toml add-member "Alice" --country Serbia

# 4. Run one collection cycle now
python3 -m jobradar.cli --config config.toml run

# 5. Or run the full daemon (scheduler + Telegram bot + dashboard)
python3 -m jobradar.cli --config config.toml serve
```

The dashboard binds to `127.0.0.1:8080` by default (IR-8). Sign in with the
dashboard token issued for your user.

### Secrets (never in config or git — DR-6)

```bash
export JOBRADAR_TELEGRAM_BOT_TOKEN=123456:ABC...      # delivery + Telegram source
export JOBRADAR_REDDIT_CLIENT_ID=...                  # Reddit OAuth (D-1)
export JOBRADAR_REDDIT_CLIENT_SECRET=...
```

Without a Telegram token the system still collects and shows everything in the
dashboard — push delivery is simply skipped (NFR-5: postings persist regardless
of Telegram availability).

## Sources

| Type | Tier | Config keys | Notes |
|---|---|---|---|
| `bluesky` | A | `query` | Public AT Protocol API, no key |
| `hn` | A | `query`, `classifier_prior` | Algolia "Who Is Hiring" search |
| `rss` | A | `url` | Any RSS/Atom feed |
| `reddit` | A | `subreddit`, `listing` | OAuth app required (D-1) |
| `telegram` | A | `channel` | Bot must be in the channel |
| `scrape` | B | `url`, `block_pattern` | Best-effort HTML; expected to break |

Enable/disable any source without code changes (SR-1):

```bash
python3 -m jobradar.cli --config config.toml sources
python3 -m jobradar.cli --config config.toml disable example-scrape
```

## Telegram bot

Members link their account with the one-time code the owner issues (IR-1):
send `/start <code>` to the bot. Routine actions are inline buttons (IR-3);
useful commands: `/status`, `/digest`, `/review`, `/mute <hours>`, `/unmute`,
`/keywords add <word>`, `/countries add <country>`. Digests are batched one per
run (FR-31); the review queue is a single batched prompt with ✅/❌ buttons
(FR-22/26). An empty digest is never sent (NFR-12).

## Compliance notes

* **Non-commercial only** (NFR-16) — a condition of Reddit's free tier.
* **Honest collection** — Tier B collectors send a fixed, truthful `User-Agent`,
  never rotate identity or use proxies (SR-5), honour `robots.txt` and HTTP 429
  (C-4), and never touch anything behind auth walls or CAPTCHAs (C-3).
* **Personal data** — the `contact` field is retained only for the life of a
  posting, excluded from logs, and never bulk-exported (DR-4). Members can
  export or fully delete their own data (DR-5).

## Tests

The full suite runs on the standard library alone and covers acceptance
criteria AC-1 through AC-9 (AC-10 is the human judgement the SRS says cannot be
automated):

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

| Test area | Requirements exercised |
|---|---|
| `test_acceptance.py` | AC-1…AC-9, SR-3/4, NFR-4/6, FR-4/19/20/27 |
| `test_units.py` | geo/eligibility, classifier, extraction, filters, dedup, digest, delivery retry, mute |
| `test_collectors.py` | Tier A/B parsing (offline) |
| `test_web.py` | auth, member isolation (NFR-8), status marking, admin gating |

## Open questions (SRS §11)

Some behaviour depends on owner decisions still open in the spec. Sensible
defaults are used and clearly marked in code so they are easy to change:

* **Eligible countries (Q-1)** — configurable per user; an empty list means
  "don't reject on geography yet" so a half-configured member still sees results.
* **Sources to monitor (Q-2/Q-3)** — defined as data in `config.toml`.
* **Stack (Q-5)** — Python 3.11 standard library, chosen for the zero-budget,
  single-host constraint.
