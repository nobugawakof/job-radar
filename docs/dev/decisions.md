# Decisions log

Newest first. Each entry says what was decided and why, so later work doesn't
undo it by accident.

## Accounts (owner's request, schema v7)
- Owner's choices: open sign-up (log in at once); the administrator can
  create, restrict and delete accounts; users link their own Telegram; users
  get their own auto-apply and their own alerts chat.
- `kof311` is the administrator: created by the migration as user 1 (owner
  of all existing data) with no password. It's reserved at sign-up, so open
  sign-up can't steal it. Its password is set from this computer (Account
  dialog after the printed link, or `jobradar admin-password`).
- Owner runs Job Radar on a machine whose screen they can't see, so the
  first admin password is claimed remotely: while kof311 has no password, a
  login as kof311 with a new password typed twice sets it (owner accepted the
  risk that someone on the network could claim it first; recovery:
  `jobradar admin-password` on the server). The login page and the console
  say when it's unclaimed. Claims are logged with the client address.
- "took too long to respond" from other computers = Windows Firewall on the
  server (its prompt appears on the server's screen); the Overview card shows
  the New-NetFirewallRule command with the real port.
- The printed `?t=` link still logs in the admin, but only from loopback; on
  any other address it's ignored. The token cookie from loopback also counts
  as the admin (keeps local tools and tests working).
- Per-user rows: user_id on applications, apply_queue, contacts,
  apply_paused, conversations, profile (tables rebuilt; SQLite can't change a
  primary key). Shared: jobs, sent, signatures, sources, runs, review queue.
- Users share the admin's Telegram API ID/hash (one app can log in many
  accounts) and the admin's bot. Alerts chat linking: `/start CODE` handled by
  the bot inbox. Users apply by Telegram only (email is the admin's mailbox)
  and have no uploaded-résumé fallback.
- Every job delivered to the admin is copied to each active user's linked
  chat (best effort). Auto-apply runs per account in sequence
  (`service.EveryoneApplier`), so a run takes longer with more users.
- The admin's Applications page shows everyone's; Messages stay private to
  each account (conversations are personal), even for the admin.
- Passwords: PBKDF2-SHA256, 240k iterations, salted. Session cookie = random
  token; only its SHA-256 is stored. Throttling: 5 failures / 15 min per
  address and per name, then 30 s doubling to 15 min; sign-ups 5 per hour per
  address.

## Dashboard on other devices (owner's request)
- Off by default; `web_allow_network` (Overview switch) binds 0.0.0.0 on the
  next start. `--host` on the command line wins over the switch.
- Login stays the secret link → HttpOnly cookie; no Host check when listening
  on the network (the cookie/token is what protects it; a DNS-rebinding page
  never has the cookie). Plain HTTP on the LAN is accepted for a trusted home
  network; the README recommends Tailscale for anything else and warns
  against port forwarding. No HTTPS: a self-signed certificate would need the
  owner to accept warnings on every device, and Tailscale already encrypts.
- Addresses come from `web/netinfo.py`; 100.64.0.0/10 is labelled Tailscale.
- QR codes: own encoder (`web/qr.py`, byte mode, versions 1-10, levels L/M),
  because the project is stdlib-only. Verified during development by
  decoding 73 codes (every version, both levels, CJK) and the on-screen code
  with jsQR; the jsQR install lived only in the scratch folder.

## Ideas taken from career-ops (owner's request)
- The owner pointed at career-ops (MIT). It uses the same overall design as
  ours: one source of facts, the AI returns JSON, deterministic rendering, and
  checks before anything is sent. The owner chose to adopt four of its ideas,
  all written independently in Python:
  1. **Title check and verb drift.** Titles stay exactly as written (only
     translated, without seniority words); bullets and the summary may not
     gain scope verbs. career-ops warns a person; Job Radar runs unattended,
     so a failing rewrite falls back to the owner's text.
  2. **Skill gaps.** existing / supported / gap without AI
     (`resume/skillgap.py`, a fixed vocabulary in English and Chinese). Shown
     in notices and the Preview, and passed to the AI as "never claim".
  3. **Page budget and paper size.** Default 2 pages, warning only (sending
     isn't blocked, since nobody is there to trim). Paper: Letter (the
     sample) / A4 / auto by the job's country via `places.find`.
  4. **Reverse-chronological order.** Experience sorted by start date; left
     alone when a date can't be read.
- Not adopted: Playwright HTML→PDF (needs a browser install), the competency
  tag grid (not in the owner's sample), interactive warnings.

## Résumé layout follows the owner's sample
- The owner supplied a sample résumé (.docx) to copy. Measured from it: US
  Letter; margins 36 pt top/bottom, 45 pt sides; name 20 pt bold #1A1A1A;
  title line 11 pt bold #1F3A5F ("title · speciality"); headings 12 pt bold
  #1F3A5F with a 1 pt rule; body 10 pt; "Role — Company" 11 pt bold (company in
  navy) with grey dates on the right; italic grey team/area line; navy "•"
  bullets; "◆ project" 10.5 pt bold with a grey "技术栈：" line; education
  "major · degree · school" on one line with grey notes; languages joined with
  " ｜ ". Headings: 职业概述, 技术技能, 工作经历, 重点项目, 教育背景, 语言能力.
- New optional fields for it: `focus` (speciality), experience `area`,
  project `stack`. Skills lines are kept verbatim (labels before "：").
- The sample uses Noto Sans CJK SC, a CFF font we can't embed (R1); Microsoft
  YaHei (with msyhbd for bold) is the closest on Windows. Real bold is used when
  a bold sibling font exists, otherwise bold is drawn by stroking.
- The sample's content stays out of the repository (personal data); only
  made-up profiles are used in tests and checks.

## Generated résumés (schema v6)
- The owner asked for résumés made automatically from a simple profile instead
  of uploading one. Required: name, job title, education; everything else is
  optional. Section order (owner's spec): name, job title, introduction, tech
  stack, work experience, projects, education, languages.
- **Owner's choices:** experience, projects, skills and languages come from
  optional profile fields (the AI may not invent them); a résumé tailored to
  each job; sent as PDF.
- No-invention guard: the AI cites a verbatim `source` for every skill and
  bullet; the source must be in the profile (in that entry's details, for
  bullets), and the numbers must be in the source. Failures fall back to the
  owner's text. Entries are referenced by index, so dates and organisations
  can't drift. The intro can't contain numbers that aren't in the profile.
- One AI call per application for the résumé, made only after the fit score
  passes; dry runs make the résumé too, so the owner can check it.
- The PDF is written with the standard library: a full TTF subsetter and PDF
  writer instead of a dependency. Fonts come from the OS; a missing Chinese
  font is a clear error, and the uploaded PDF is the fallback.
- The profile is stored in the database (personal data, not secrets) so it's
  backed up; generated PDFs aren't backed up.
- `resume_source = "generated"` is the default. With an incomplete profile, an
  uploaded PDF is still used, so existing setups keep working.
- The profile name signs messages (`applicant_name` is the fallback).

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
