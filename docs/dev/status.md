# Status

Update this file whenever an item is finished or a new one is found. The IDs
come from the audit the owner works through one item at a time; keep them.

## Done (merged or on the working branch)

- Auto-apply (Telegram account + email, dry run/auto, AI or template, résumé PDF).
- Chinese-aware dedup; reworded reposts across runs.
- Web dashboard; SQLite storage.
- B1–B9 bug fixes; B5 bot inbox and public channels via the account.
- S1 review queue.
- `jobradar-data` folder; one-file backup and restore (manual; S8 automatic backups still open).
- Conversations with representatives mirrored from Telegram, Messages tab,
  replies from the dashboard, Chinese/English messages with Chinese the default.
- Résumés generated from a Profile page (required: name, job title, education),
  tailored per job by AI with no-invention checks, as PDF with an embedded
  Chinese font; preview, import from an old résumé, `jobradar resume`.
- Résumé layout copied from the owner's sample; title/verb guards, skill-gap
  report, page limit and paper size, newest-first order (ideas from career-ops).

## Open

### Needs real accounts (can't be tested in the cloud sandbox)
| # | Item |
|---|---|
| V1 | Telegram bot: sending jobs, "Find my chat", review buttons |
| V2 | Telegram account: login, applying, reading channels, **conversation sync and replies** |
| V3 | Gemini and Claude calls, including Chinese drafts, **résumé tailoring and résumé import** |
| V4 | SMTP email |
| V5 | Live sites (HN, WeWorkRemotely, Reddit, V2EX, Ruby China, Bluesky; Bluesky search may need a login) |
| V6 | The Windows .exe on real Windows, **including finding Microsoft YaHei/SimHei for the résumé PDF** |
| V8 | Résumé PDFs opened in real viewers (Telegram preview, WeChat, Acrobat, phones); checked here only with poppler |
| V7 | AC-10: a week of real use |

### Not built
| # | Item |
|---|---|
| M1 | Reading email replies (IMAP) into conversations |
| R1 | CFF-based fonts (.otf, e.g. Noto Sans CJK) for the résumé PDF |
| R2 | Résumé photo, and more than one layout |
| S2 | Several users |
| S3 | Interactive bot commands (beyond the review buttons) |
| S4 | Browsing, filtering and marking jobs in the dashboard |
| S5 | Storing raw fetched content; keeping rejected posts for 7 days |
| S6 | All job record fields from SRS 5.1 |
| S7 | Delete jobs after 90 days (currently count-based) |
| S8 | Automatic database backups |
| S9 | A full check finishes within 15 minutes (apply pacing can exceed it) |
| S10 | Extraction rules kept in settings |
| S11 | Telegram account setup is still technical (my.telegram.org) |
| S12 | Rank alerts by résumé fit |

### Owner's decision needed
D1–D5 (see decisions.md).

### Docs and tests
| # | Item |
|---|---|
| E1 | `docs/SRS.md` is outdated (Draft 0.1); open questions Q-1 to Q-4 and Q-7 |
| E2 | No direct tests for filters, extraction, regions and site reading |
| E3 | Saving from the dashboard drops comments in `config.toml` (a `.bak` copy is kept) |
