# Decisions log

Newest first. Each entry says what was decided and why, so later work doesn't
undo it by accident.

## Conversations and language (schema v5)
- **The real conversation lives in the owner's Telegram account. Job Radar mirrors it.**
  - Applications are sent from that account, so the owner can also continue
    from their phone.
  - The sync reads only chats with contacts Job Radar applied to (the
    `conversations` rows), using `min_id` from the last stored message id.
  - Messages the owner sends from the phone are stored as `out`.
- One conversation per representative, `(channel, contact)`, even across jobs.
  Each message records which job it was about.
- Sync runs after every check (lock timeout 60 s), every 300 s in the dashboard,
  and on "Check for replies". New incoming messages are unread and trigger a 💬
  bot notice.
- **Email replies are not read** (that would need IMAP). Sent mail and
  dashboard replies are recorded, and replies use `Re: <original subject>`.
- Dry runs store the draft in `applications.message` but create no conversation.
- `message_language`: `zh` (default, the owner's request), `en`, or `auto`
  (CJK ratio of the post).
  - With `zh`, the AI writes Simplified Chinese even for English posts and
    keeps technical terms as they are.
  - Templates: `apply_template_zh` / `apply_email_subject_zh` and the English
    `apply_template` / `apply_email_subject`.
  - English-path tests must set `message_language="en"`.

## Data folder and backup
- Everything lives in `jobradar-data/`, and old loose files are migrated
  automatically.
- `config.toml` and the Telegram session stay as separate files on purpose.
  Secrets are kept out of the shareable database, and the session file belongs
  to Telethon.
- Backup is one zip. Restore moves replaced files aside and never deletes them.
- Checking whether a corrupt database is in use opens it read-only through a
  URI, so the check can't change the file.

## Telegram channels and bot inbox (B5)
- A single bot inbox reads `getUpdates`, confirms them and persists the offset.
  The old code re-read the same 100 updates forever.
- Public channels are read through the owner's account (no need to join).
  Without an account, the bot's channels are used.

## Review queue (S1)
- When regions are set, jobs that don't state a location go to a review queue
  instead of being dropped or sent (spec FR-20).
  - The owner reviews them with ✅/❌ in Telegram or the dashboard.
  - Unanswered jobs expire after 14 days.
  - Setting `unknown_location = "send"` restores the old behaviour.

## Filtering fixes
- **B4:** regions compare sets of countries, so groupings like EU, EMEA and APAC
  work. Jobs with no stated location aren't silently dropped. A "City, ST"
  pattern catches US on-site jobs; on-site with an unknown place is rejected;
  ambiguous US state codes are excluded.
- **B3:** salaries are parsed in any currency and Chinese formats (月薪 20-30K,
  2万, 16薪), and numbers that aren't pay are skipped. Whether ¥ means CNY or
  JPY depends on the place.
- **B2:** "global", "international" or "anywhere" in a company blurb doesn't make
  a job worldwide.
- **B1:** each source fails on its own. An XHTML error page is not an empty
  feed: the parser checks the root tag.

## Small fixes (B6–B9)
- **B6:** contacts are masked in logs.
- **B7:** the age limit defaults to 15 days, matching the docs.
- **B8:** one check at a time across processes, with a stale lock taken over.
- **B9:** run history is persisted.

## Dedup
- Chinese text uses character shingles. Reworded reposts are caught across runs
  with MinHash/LSH plus a role guard, so different roles at one company aren't
  merged.

## Storage
- SQLite (stdlib) replaced the JSON state file. The old JSON is imported once
  and renamed `.migrated`.

## Auto-apply (owner's choices)
- The owner chose: dry run first, then auto; AI-tailored messages with a
  template fallback; attach the PDF; Telegram first, then email.
- Safety rails:
  - each posting once;
  - a 30-day cooldown per contact;
  - a daily cap of 10 per 24 h;
  - a random 45–120 s gap between sends;
  - back off on PEER_FLOOD / FLOOD_WAIT.

## Deliberate spec differences (D1–D5, awaiting the owner's decision)
- **D1:** one message per job, not a digest.
- **D2:** a single 403 is temporary.
- **D3:** Reddit via RSS.
- **D4:** Claude is optional and paid.
- **D5:** auto-apply and AI aren't in the spec.
