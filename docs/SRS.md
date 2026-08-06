# Software Requirements Specification

## Social Job Radar

**Version:** 0.1 (Draft)
**Date:** 6 August 2026
**Status:** For review — open questions listed in Section 11
**Author:** Project owner (original concept)

---

## 1. Introduction

### 1.1 Purpose

This document specifies the requirements for **Social Job Radar**, a system that automatically collects job postings published on social platforms, filters them against a user's interests and work eligibility, and delivers the surviving results through a Telegram bot and a web dashboard.

The document is intended for the project owner acting as developer, and for any collaborator joining later. It defines *what* the system must do and *why*; it does not prescribe implementation.

### 1.2 Scope

Many technology roles — particularly in Web3, AI, and remote-first startups — are advertised only in social posts and community threads, never on formal job boards. These postings are unstructured, short-lived, and buried in unrelated conversation. Finding them currently requires manually reading multiple feeds every day.

Social Job Radar automates that reading. It monitors a configured set of social sources, extracts anything that looks like a job posting, discards what does not match the user's keywords, remote requirement, and work eligibility, and pushes the remainder to the user within hours of publication.

### 1.3 Release phasing

The project is deliberately split. Phase boundaries are firm; requirements below are tagged accordingly.

| Phase | Scope | Status |
|---|---|---|
| **Phase 1** | Collection, extraction, rule-based filtering, review queue, delivery | Specified in this document |
| **Phase 2** | Resume ingestion and relevance ranking of collected postings | Outlined only (Section 10) |

Phase 1 is a **collector**, not a matcher. It answers "what job posts exist that fit my stated criteria?" Phase 2 will answer "which of them fit *me* best?" Phase 1 must therefore store enough raw posting text for Phase 2 to score against later.

### 1.4 Definitions

| Term | Meaning |
|---|---|
| **Posting** | A social post judged by the system to be a job advertisement |
| **Source** | One monitored origin (a subreddit, a Telegram channel, a Bluesky feed, a scraped page) |
| **Collector** | The component that fetches raw content from one source type |
| **Extractor** | The component that parses raw post text into structured posting fields |
| **Eligible country** | A country the user is legally permitted to work in |
| **Hiring geography** | The set of locations from which an employer will hire for a posting |
| **Review queue** | Postings held for user confirmation instead of being delivered or discarded |
| **Run** | One scheduled execution of the collection pipeline across all enabled sources |

### 1.5 References

- Reddit Data API terms and Responsible Builder Policy (November 2025)
- Bluesky AT Protocol public HTTP API documentation
- Telegram Bot API documentation
- Hacker News Search API (Algolia)

---

## 2. Overall Description

### 2.1 Product context

Social Job Radar is a self-hosted, standalone application. It has no dependency on any commercial service, no paid API subscriptions, and no cloud infrastructure. It runs on the project owner's personal computer and serves a small, closed group of users.

### 2.2 User classes

| Class | Count | Characteristics | Needs |
|---|---|---|---|
| **Owner** | 1 | Technical; runs and maintains the host machine | Full configuration, source management, log access, user administration |
| **Member** | 2–10 | Friends of the owner; non-technical assumed | Own keyword set, own eligible-country list, own delivery preferences, own feed |

There is no public registration. Members are added by the owner.

### 2.3 Operating environment

- **Host:** the owner's personal laptop or desktop
- **Availability:** intermittent — the machine sleeps, closes, and disconnects unpredictably
- **Network:** ordinary residential connection, single IP address
- **Budget:** €0 / $0 recurring. No paid API tier, no paid hosting, no paid proxy service.

The intermittent host is a first-class design constraint, not an edge case. See NFR-4.

### 2.4 Assumptions and dependencies

**Assumptions**

- A-1: Users can install and use Telegram.
- A-2: The web dashboard is accessed from the local network or over a tunnel; it is not publicly exposed.
- A-3: Posting volume across all sources will not exceed a few thousand candidate posts per day.
- A-4: The group remains non-commercial. No user is charged, and no collected data is resold.

**Dependencies**

- D-1: Reddit access requires an approved OAuth application under the Responsible Builder Policy. Approval is manual and has taken two to four weeks. **This must be applied for at project start**, as it is on the critical path.
- D-2: Telegram bot delivery requires a bot token from BotFather.
- D-3: Best-effort scraped sources depend on third-party markup that may change without notice.

### 2.5 Constraints

- C-1: No component may require a paid subscription or per-call billing.
- C-2: The system must run on a single machine without a static IP or inbound port forwarding.
- C-3: Collection must not access content behind authentication walls, and must not attempt to circumvent CAPTCHAs, bot-detection challenges, or request-signing schemes.
- C-4: Scraped sources must be fetched at a rate no greater than a human browsing the same pages, and must honour `robots.txt` and HTTP `429` / `Retry-After` responses.
- C-5: Reddit access is limited to non-commercial use at 100 queries per minute per OAuth client.

---

## 3. Data Sources

Sources differ enormously in accessibility. The system must treat this as a structural property, not an implementation detail: each source is assigned a **tier** that determines the reliability guarantees it carries and how failures are handled.

### 3.1 Source tiers

**Tier A — Supported.** Documented, permitted, free access. Expected to work continuously. A Tier A failure is a defect.

| Source | Access method | Notes |
|---|---|---|
| Reddit | Official Data API, OAuth | Free at 100 QPM, non-commercial only; requires prior approval |
| Bluesky | Public AT Protocol HTTP API | No key required; substantial tech-hiring activity |
| Telegram channels | Bot API | Free; strong coverage of regional developer job channels |
| Hacker News | Algolia search API | Free; "Who Is Hiring" monthly threads |
| RSS / Atom feeds | Direct HTTP | Any source publishing a feed |

**Tier B — Best effort.** Publicly reachable pages fetched over plain HTTP and parsed. Permitted by no platform's terms of service; expected to break. A Tier B failure is **not** a defect — it is anticipated behaviour.

Tier B exists because the owner has accepted the trade-off knowingly. The requirements below are written to contain the resulting fragility rather than pretend it is absent.

**Tier C — Blocked.** No free, technically viable path exists at the time of writing.

| Platform | Obstacle |
|---|---|
| X / Twitter | Free API tier discontinued February 2026; reads billed per request. Logged-out pages serve no searchable content. |
| Facebook | No third-party public post search exists at any price. |
| TikTok | Research API restricted to accredited academic researchers; content is JavaScript-rendered behind request signing. |

Tier C platforms remain recorded in the specification so the decision is documented and revisitable, but no Phase 1 requirement depends on them.

### 3.2 Source requirements

- **SR-1:** Each source shall be independently enabled or disabled without code changes.
- **SR-2:** Each source shall declare its tier. The system shall display the tier to users alongside results originating from it.
- **SR-3:** A failing source shall never halt the run. Other sources shall complete normally.
- **SR-4:** A Tier B source failing on three consecutive runs shall be automatically disabled and the owner notified. This prevents a broken scraper from silently hammering a host.
- **SR-5:** Tier B collectors shall identify themselves honestly in the `User-Agent` header and shall not rotate identities, spoof fingerprints, or use proxies to evade blocking.
- **SR-6:** Per-source request rate shall be configurable, defaulting to no more than one request every five seconds.

---

## 4. Functional Requirements

### 4.1 Collection

- **FR-1:** The system shall execute a scheduled collection run at a configurable interval, default every four hours.
- **FR-2:** Each run shall query every enabled source for content published since that source's last successful run.
- **FR-3:** The system shall record, per source, the timestamp of the last successfully collected item.
- **FR-4:** On startup, the system shall detect any collection window missed while the host was asleep or offline and shall immediately perform a catch-up run covering that gap. *(This requirement exists because the host is a personal laptop; without it, every overnight posting is lost.)*
- **FR-5:** Catch-up runs shall be capped at a configurable lookback, default 72 hours, to bound the cost of a long outage.
- **FR-6:** All raw fetched content shall be stored before parsing, so that extraction logic can be improved and re-run against historical data without re-fetching.

### 4.2 Job detection

- **FR-7:** The system shall classify each collected post as *job posting* or *not a job posting*.
- **FR-8:** Classification shall favour recall over precision. A missed posting is invisible to the user and therefore unrecoverable; a false positive is merely noise the user can dismiss.
- **FR-9:** Posts classified as non-postings shall be retained for a configurable period (default 7 days) to permit tuning of the classifier.

### 4.3 Extraction

- **FR-10:** For each detected posting, the system shall extract the fields defined in Section 5.
- **FR-11:** Any field that cannot be extracted with confidence shall be recorded as *unknown* rather than guessed.
- **FR-12:** The original unmodified post text shall always be stored, regardless of extraction success. Extraction is lossy; the source text is the record of truth and the input to Phase 2.

### 4.4 Filtering

Filters are applied in order. A posting must pass all three to be delivered.

**Keyword filter**

- **FR-13:** Each user shall configure a keyword set. Default set: `web3`, `web2`, `backend`, `frontend`, `ai`, `fullstack`.
- **FR-14:** A posting matching at least one keyword passes. Matching shall be case-insensitive and shall account for common variants (e.g. `full-stack`, `full stack`, `fullstack`).

**Remote filter**

- **FR-15:** Each user shall configure whether only remote roles are of interest. Default: remote only.
- **FR-16:** When remote-only is enabled, a posting passes if it is explicitly remote, or if its work arrangement is unknown. Postings explicitly requiring on-site or hybrid presence are rejected.

**Eligibility filter**

- **FR-17:** Each user shall configure a list of countries they are legally permitted to work in.
- **FR-18:** A posting passes if its hiring geography **intersects** the user's eligible countries.
- **FR-19:** A posting whose hiring geography is stated as worldwide, global, or "anywhere" shall **pass** for every user. *(A naive country-name match would reject exactly the postings most likely to be relevant to a remote worker. This requirement exists to prevent that failure.)*
- **FR-20:** A posting whose hiring geography cannot be determined shall be routed to the review queue (FR-21), never silently discarded.

### 4.5 Review queue

- **FR-21:** Postings with undetermined hiring geography shall be placed in a per-user review queue.
- **FR-22:** The system shall prompt the user through the Telegram bot to confirm or reject each queued posting, presenting the posting's title, source, and text excerpt.
- **FR-23:** The user shall be able to resolve a queued posting as *relevant* or *not relevant* with a single interaction.
- **FR-24:** The system shall learn from resolutions: repeated rejections of postings sharing an attribute (a source, an employer, a phrase) shall be surfaced to the user as a suggested filter rule.
- **FR-25:** Unresolved queue items shall expire after a configurable period, default 14 days.
- **FR-26:** Review prompts shall be batched, never sent one message per posting, to avoid notification fatigue.

### 4.6 Deduplication

- **FR-27:** The same job posted to multiple sources, or reposted to one source, shall be delivered to a user only once.
- **FR-28:** Duplicate detection shall use near-match comparison of posting text and any contained application URL, not exact string equality.
- **FR-29:** When duplicates are merged, all originating sources shall be retained and shown on the single delivered record.

### 4.7 Delivery

- **FR-30:** Matched postings shall be delivered through a Telegram bot and made available in a web dashboard. Both channels shall reflect the same underlying data.
- **FR-31:** Telegram delivery shall be batched into a digest per run rather than one message per posting.
- **FR-32:** Each delivered posting shall link back to the original post.
- **FR-33:** Users shall be able to mute delivery for a chosen period without losing collection — muted postings accumulate and remain available in the dashboard.
- **FR-34:** The dashboard shall support filtering and sorting the user's collected postings by date, source, keyword, and salary presence.
- **FR-35:** Users shall be able to mark a posting as *applied*, *saved*, or *dismissed*, and dismissed postings shall not reappear.

### 4.8 Configuration and administration

- **FR-36:** Each user shall independently configure keywords, eligible countries, remote preference, and delivery schedule.
- **FR-37:** The owner shall be able to add and remove members.
- **FR-38:** The owner shall have access to a run history showing, per run and per source, items fetched, postings detected, and errors raised.
- **FR-39:** The owner shall be able to trigger a manual run on demand.

---

## 5. Data Requirements

### 5.1 Posting record

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | UUID | Yes | Internal identifier |
| `title` | text | Yes | Role title; falls back to a generated summary if the post has no clear title |
| `description` | text | Yes | Full original post text, unmodified |
| `contact` | text | No | Application route: email, form URL, or platform handle |
| `location` | text | No | Stated work location or hiring geography |
| `is_remote` | enum | Yes | `remote` / `hybrid` / `onsite` / `unknown` |
| `salary_raw` | text | No | Compensation exactly as written |
| `salary_min`, `salary_max`, `salary_currency`, `salary_period` | mixed | No | Parsed compensation where possible |
| `source` | text | Yes | Originating source identifier |
| `source_tier` | enum | Yes | A / B |
| `source_url` | URL | Yes | Link to the original post |
| `posted_at` | timestamp | Yes | Publication time; collection time if unavailable |
| `collected_at` | timestamp | Yes | When the system retrieved it |
| `matched_keywords` | list | Yes | Which keywords caused it to pass |
| `status` | enum | Yes | `new` / `pending_review` / `delivered` / `applied` / `saved` / `dismissed` / `expired` |
| `duplicate_of` | UUID | No | Set when merged into another record |

Salary is stored twice deliberately: `salary_raw` preserves what was written ("competitive, DOE + tokens"), while the parsed fields support sorting and filtering when the format permits. Neither substitutes for the other.

### 5.2 Storage and retention

- **DR-1:** All data shall be stored locally on the host. No third-party data store shall be used.
- **DR-2:** A single-file embedded database is sufficient and preferred, given the scale and the requirement to run without external services.
- **DR-3:** Postings not marked `applied` or `saved` shall be purged after a configurable retention period, default 90 days.
- **DR-4:** The `contact` field constitutes personal data. It shall be retained only for the life of the posting record, shall never be exported in bulk, and shall be excluded from any logs. If any user's eligible countries include the EU or UK, this field brings the system within the scope of data-protection law even at personal scale — minimising its retention is the mitigation.
- **DR-5:** The system shall support a full data export and full deletion for any individual member on request.
- **DR-6:** Credentials and API tokens shall be stored outside the database and outside version control.

---

## 6. Interface Requirements

### 6.1 Telegram bot

- **IR-1:** Members shall link their Telegram account to their profile via a one-time code issued by the owner.
- **IR-2:** The bot shall support, at minimum: view latest digest, review pending queue items, edit keywords, edit eligible countries, mute and unmute, and show status.
- **IR-3:** All routine interactions shall be available through inline buttons; typed commands shall not be required for daily use.
- **IR-4:** Digest messages shall be readable on a phone screen without expansion — title, salary if known, source, and link.

### 6.2 Web dashboard

- **IR-5:** The dashboard shall authenticate users and show only their own postings and settings.
- **IR-6:** The dashboard shall provide the browsing, filtering, and status-marking capabilities in FR-34 and FR-35.
- **IR-7:** The dashboard shall present the owner's administrative views (FR-37 to FR-39), hidden from members.
- **IR-8:** The dashboard shall bind to localhost by default. Any wider exposure shall be an explicit configuration choice.

---

## 7. Non-Functional Requirements

### 7.1 Performance

- **NFR-1:** A full collection run across all enabled sources shall complete within 15 minutes under normal conditions.
- **NFR-2:** Dashboard pages shall render within 2 seconds for a database of up to 100,000 postings.
- **NFR-3:** The system shall remain within Reddit's 100 QPM limit at all times, with a safety margin.

### 7.2 Reliability

- **NFR-4:** The system shall tolerate arbitrary host shutdown at any point. An interrupted run shall leave no partial or corrupted records, and the next startup shall resume cleanly.
- **NFR-5:** No posting that passes filtering shall be lost. Delivery failures shall be retried; postings shall persist in the dashboard regardless of Telegram availability.
- **NFR-6:** A single malformed post shall not abort processing of the batch containing it.
- **NFR-7:** The database shall be backed up automatically to a local path on a configurable schedule.

### 7.3 Security

- **NFR-8:** Member accounts shall be isolated. No member shall be able to view another member's postings, settings, or queue.
- **NFR-9:** All outbound requests shall use HTTPS.
- **NFR-10:** Secrets shall never be written to logs or error messages.

### 7.4 Usability

- **NFR-11:** A non-technical member shall be able to complete setup — link Telegram, set keywords, set countries — without assistance beyond the bot's own prompts.
- **NFR-12:** The system shall never deliver an empty digest. Silence means nothing matched.

### 7.5 Maintainability

- **NFR-13:** Adding a new source shall require implementing a single well-defined collector interface, with no changes to filtering, storage, or delivery.
- **NFR-14:** Extraction rules shall be expressed as data or configuration wherever practical, so they can be tuned without redeployment.
- **NFR-15:** Tier B collectors shall be isolated such that a breaking change in one has no effect on any other component.

### 7.6 Legal and compliance

- **NFR-16:** The system shall be used non-commercially. No member shall be charged, and collected data shall not be redistributed or sold. This is a condition of Reddit's free tier.
- **NFR-17:** Tier B collection is understood to conflict with the terms of service of the platforms concerned. The accepted consequence is loss of access to those platforms. The system shall be architected so that this loss degrades functionality without disabling it.
- **NFR-18:** The system shall not collect from any source that has explicitly blocked it, and shall treat a block as permanent until manually re-enabled.

---

## 8. Acceptance Criteria

Version 1.0 is complete when all of the following hold:

| # | Criterion |
|---|---|
| AC-1 | Two or more Tier A sources collect successfully on a repeating schedule |
| AC-2 | Closing the laptop overnight and reopening it triggers a catch-up run that recovers the missed window |
| AC-3 | A posting stating "remote, worldwide" is delivered to a user whose eligible-country list does not name any country in the post |
| AC-4 | A posting with no location information appears in the review queue and can be resolved from Telegram |
| AC-5 | The same job posted to two sources is delivered once, showing both origins |
| AC-6 | Two members with different keyword sets receive demonstrably different digests |
| AC-7 | Disabling every Tier B source leaves the system fully functional on Tier A alone |
| AC-8 | A deliberately broken Tier B source auto-disables after three failures and notifies the owner, with all other sources unaffected |
| AC-9 | Killing the process mid-run leaves the database consistent and the next run correct |
| AC-10 | Over one week of live running, the owner judges the delivered postings worth reading — the true success measure, and the only one that cannot be automated |

---

## 9. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-1 | Reddit OAuth approval delayed or refused | Medium | High | Apply on day one; Bluesky, Telegram, and HN carry the system if refused |
| R-2 | Tier B sources blocked | **High** | Medium | Accepted by design; Tier A is the load-bearing path (AC-7) |
| R-3 | Signal-to-noise too low to be useful | Medium | High | Recall-first classification plus review-queue learning (FR-24); Phase 2 ranking is the structural fix |
| R-4 | Host too often offline to be timely | Medium | Medium | Catch-up runs (FR-4); if it persists, a free-tier always-on host is the escape route |
| R-5 | Volume overwhelms the user | Medium | Medium | Batched digests, mute, dismissal |
| R-6 | Personal data collected via `contact` field | Low | Medium | Minimal retention, no export, no logging (DR-4) |
| R-7 | Free tier terms change again | Medium | High | Source abstraction (NFR-13) keeps replacement cheap |
| R-8 | Project outgrows "non-commercial" | Low | High | Any move to charging requires re-licensing Reddit access first |

---

## 10. Phase 2 Outline

Not specified here; recorded so Phase 1 does not foreclose it.

Phase 2 introduces resume ingestion — upload or profile form, format to be decided — and replaces binary keyword filtering with graded relevance scoring of each posting against the user's actual experience. Postings would then arrive ranked rather than merely filtered.

Phase 1 enables this by storing full original post text for every posting (FR-12) and by retaining rejected posts for a period (FR-9), giving Phase 2 a labelled corpus to develop and evaluate against. No Phase 2 requirement is committed to at this stage.

---

## 11. Open Questions

Items requiring the owner's decision before implementation begins.

| # | Question | Blocks |
|---|---|---|
| Q-1 | **Which countries are you eligible to work in?** The eligibility filter (FR-17 to FR-19) cannot be implemented or tested without this list. | FR-17, AC-3 |
| Q-2 | Which specific subreddits, Telegram channels, and Bluesky feeds should be monitored at launch? | SR-1 |
| Q-3 | Which Tier B pages are worth attempting first, given the expected failure rate? | Tier B scope |
| Q-4 | How many members at launch, and who? | FR-37 |
| Q-5 | Preferred implementation language and stack? | All |
| Q-6 | Is a local-only dashboard acceptable, or is remote access needed from a phone? | IR-8 |
| Q-7 | Is a project name preferred over the working title "Social Job Radar"? | — |

---

*End of specification. Draft 0.1 — expected to change as open questions close.*
