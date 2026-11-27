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
`jobradar.db`, in the `jobradar-data` folder. SQLite is built into Python, so there's no
database server to install or run. The file holds:

| Table | What it remembers |
|---|---|
| `sent` | Fingerprints of every job already sent, so nothing is sent twice |
| `jobs` | Each delivered job: title, link, source, salary, location and the full original text |
| `signatures` | Similarity signatures of sent jobs, to recognise reworded reposts |
| `sources` | Each source's last successful check, failure count, and whether it's switched off |
| `applications`, `apply_queue`, `contacts`, `apply_paused` | Auto-apply history, waiting jobs, who was contacted, and paused channels (per account) |
| `conversations`, `messages`, `profile` | Each account's conversations with recruiters and résumé profile |
| `users`, `sessions` | Accounts (passwords stored hashed) and logged-in browsers |
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

Keep the window open: closing it stops Job Radar. The dashboard is on port 80
and other computers can open it too (see below); everyone logs in with an
account. On the server itself, the link the program prints logs you in as the
administrator. Secrets are never shown back in full.

### Open the dashboard from other computers and the internet

Job Radar listens on **port 80 on every network**, so other computers open it
at `http://<server address>/` and log in (see **Accounts** below). There's
nothing to switch on. The window prints the addresses; the Overview page shows
them with a QR code.

**What has to let it through**
1. **Windows Firewall.** When Job Radar runs **as administrator**, it opens
   port 80 in Windows Firewall itself (rule "Job Radar dashboard") and says so
   on the Overview page. Otherwise it shows the one command to run in
   PowerShell as administrator.
2. **Port 80 must be free.** IIS uses port 80 when it's installed. If so, Job
   Radar starts on 8080 instead and says so. To get port 80, stop or move IIS's
   site: IIS Manager → Sites → **Default Web Site** → Bindings (change 80 to
   another port) or **Stop**, then restart Job Radar.
3. **On a cloud server** (AWS, Azure, Google Cloud…), the address Windows
   knows (e.g. `10.x.x.x`) is private. From outside use the server's
   **public** address, the one you use for Remote Desktop, and allow port 80
   in the cloud's own firewall (AWS: EC2 → Instances → Security → security
   group → Edit inbound rules; often already open for IIS).

**Tailscale (optional)** is a free, encrypted private network between your
own devices, for when you can't open ports: install it on the server and your
PC with the same account, and the Overview page shows a **Through Tailscale**
address (`100.…`).

**Keep it safe on the internet**
- Set the administrator password **first**: log in as `kof311` (password typed
  twice) right after starting Job Radar, before anyone else does.
- Anyone who can reach the page can create an account; restrict or delete
  accounts on the **Users** page.
- The page uses plain HTTP, so passwords cross the internet unencrypted. Use
  only for accounts you'd accept that for, or put it behind HTTPS.
- `python3 -m jobradar.cli web --host 127.0.0.1` keeps it to this computer only.

### Accounts: administrator and users

Job Radar can be shared: each person has an account with their own profile,
Telegram account, job alerts, applications and conversations.

**The administrator is `kof311`.** Nobody else can register that name.
- **First login sets the password.** Until kof311 has a password, the first
  person to log in as `kof311`, from any computer, chooses it (typed twice).
  The login page says so. Do this right after starting Job Radar, so no one
  else gets there first. After that it's a normal login.
- On the computer that runs Job Radar, the link it prints also logs you in as
  kof311. You can change the password under **Account** (top right), or reset
  it on that computer with `python3 -m jobradar.cli admin-password`.
- The administrator sees and manages everything: the bot, job search, AI keys,
  auto-apply settings, review queue, activity, everyone's applications, and
  the **Users** page.

**Users sign up themselves** (**Create account** on the login page) and are
logged in at once. A user only sees:
- **Profile**: the facts their résumé is made from;
- **My Telegram**:
  1. link their own Telegram account (phone → code; the administrator must
     have entered the Telegram API ID/hash);
  2. link their alerts chat: **Link my bot chat** opens the Job Radar bot and
     sends `/start CODE`;
  3. see whether auto-apply is on for them;
- **Applications** and **Messages**: their own only.

Everything else (sites, keywords, AI keys, limits, the bot) is the
administrator's.

**What each user gets**
- Every job the shared search finds goes to their alerts chat too.
- With auto-apply on (the administrator's switch), each user with a linked
  Telegram account and a complete profile gets their own applications: a
  résumé made from *their* profile, sent from *their* Telegram account, with
  their own daily cap, cooldowns and "applied" notices.
- Users apply by Telegram only; email goes out from the administrator's
  mailbox, so it's used for the administrator's applications only.
- Replies from recruiters show up in each user's own **Messages**.

**Managing accounts (Users page)**
- **Create** an account with a first password.
- **Restrict**: the user is logged out at once, can't log in, gets no alerts,
  and nothing is applied for them. **Allow** undoes it.
- **New password**: set a new password and log them out everywhere.
- **Delete**: removes the account, its profile, applications, conversations,
  résumés and Telegram login. The administrator can't be restricted or
  deleted.

**Security**
- Passwords are stored salted and hashed (PBKDF2), never as text.
- Sessions last 30 days, in a private cookie. Repeated wrong passwords are
  slowed down, and so are many sign-ups from one device.
- A user's Telegram login is stored on the computer that runs Job Radar, so
  whoever runs it could use it. The My Telegram page says so before anyone
  links an account.

**Your files** all live in one folder, `jobradar-data`, beside the program:

| File | What it is |
|---|---|
| `config.toml` | Settings and keys. Kept out of the database on purpose (spec DR-6), so sharing the database never leaks a key |
| `jobradar.db` | History: sent jobs, applications (with the message sent), your conversations with representatives, your profile, check history |
| `resumes/` | The résumé PDF made for each application (and the latest preview) |
| `fonts/` | Optional: a font for the résumé PDF, if your computer has no Chinese TrueType font |
| `jobradar-user.session` | Your Telegram login, if you logged in. Full access to your account: keep it private |
| `sessions/user-<n>.session` | Each user's Telegram login (same warning) |
| `resume.pdf` / `resume.txt` | Your résumé, if uploaded |
| `jobradar-web.token` | The dashboard's private session-cookie secret |

Older versions kept these files loose beside the program; they're moved into
the folder automatically the first time. You never need to open them, because
the dashboard manages them all.

**Backup and restore:** on the Overview page, **Download backup** saves
settings, history and résumé as one `.zip`. Tick the box to include your
Telegram login, but only when moving to another computer. **Restore from a
backup…** puts it back; the files it replaces are moved to a
`before-restore-<time>` folder, never deleted. Without the dashboard:

```bash
python3 -m jobradar.cli backup                       # writes jobradar-backup-<date>.zip
```

```bash
python3 -m jobradar.cli restore jobradar-backup-20261126-0930.zip
```

## Quick start — command line

Requires Python 3.11+. Nothing to install.

```bash
# 1. Configure — just fill in the two Telegram lines; the rest has defaults
mkdir -p jobradar-data && cp config.example.toml jobradar-data/config.toml   # edit the two Telegram lines

# 2. Check the wiring
python3 -m jobradar.cli test-telegram

# 3. Run one cycle now…
python3 -m jobradar.cli run

# 4. …or run forever at the configured interval
python3 -m jobradar.cli serve
```

There is a **single** config file — `jobradar-data/config.toml`, copied from
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
| `web [--no-browser]` | Open the dashboard (port 80, reachable from other computers; `--host 127.0.0.1` = this computer only) |
| `run` | One collection cycle → one Telegram digest |
| `serve` | Loop forever, running every `run_interval_hours` |
| `sources` | List configured sources and their state |
| `test-telegram` | Send a test message to the configured chat |
| `telegram-login` | Log **your own** Telegram account in, once (for auto-apply) |
| `test-apply [--posting FILE] [--send]` | Draft an application for a sample (or real) posting; `--send` delivers it to yourself |
| `resume [--posting FILE] [-o FILE]` | Make your résumé PDF from your profile (tailored to a posting, with AI) |
| `admin-password` | Set or reset the administrator (kof311) password |
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
   ─▶ draft (Chinese by default) ─▶ dry_run: show it to you   |   auto: send + résumé
   ─▶ record (never the same posting twice, never the same person twice in 30 days)
   ─▶ a conversation with that person opens in the dashboard's Messages tab
```

### Set it up

1. **Your profile → your résumé.** On the dashboard's **Profile** page, enter
   your details; Job Radar makes the résumé PDF from them (see
   [Résumés made from your profile](#résumés-made-from-your-profile)). Or set
   **Résumé to send → My own PDF** and upload one (`resume_source = "uploaded"`).
2. **AI (recommended).** Set `use_ai = true` and a Gemini or Claude key (see
   above). For each posting the AI scores how well your résumé fits (0–100) and
   writes a short, specific message. It uses only facts from your résumé, in
   your message language (below). Postings below `apply_min_score` are skipped.
   Without AI, `apply_keywords` is required and every application uses your
   fixed template (`apply_template_zh` in Chinese, `apply_template` in English).
3. **Telegram account** (for Telegram contacts). A bot can't message people who
   never started it, so this logs in as *you*:
   ```bash
   pip install telethon
   ```
   Get `telegram_api_id` / `telegram_api_hash` at <https://my.telegram.org> →
   *API development tools*, enter them in the dashboard (or `config.toml`), then log in there, or run once:
   ```bash
   python3 -m jobradar.cli telegram-login
   ```
   It asks for the code Telegram sends you (and your 2FA password, if set) and
   saves `jobradar-data/jobradar-user.session`. **Keep that file private: it gives full access
   to your Telegram account.** It's git-ignored.
4. **Email** (for email-only postings). Fill in the `smtp_*` settings. For
   Gmail, use `smtp.gmail.com:587` with an
   [App Password](https://myaccount.google.com/apppasswords).
5. **Check the wiring:**
   ```bash
   python3 -m jobradar.cli test-apply            # prints the draft
   python3 -m jobradar.cli test-apply --send     # sends it to YOU
   ```
   `--send` delivers it to your Telegram *Saved Messages* and your own inbox,
   so you see exactly what a hiring manager would get. Use `--posting job.txt`
   to draft against a real posting.

### Résumés made from your profile

You don't need a résumé file. On the **Profile** page you enter:

| Required | Optional |
|---|---|
| Name, job title, education (at least one school) | Speciality (shown after the title), gender, date of birth, location, phone, email, professional summary, technical skills, work experience (role, company, team/area, dates, points), key projects (name, tech stack, points), languages |

Each application then gets a **PDF résumé made for that job**, in this order:
name → job title · speciality → 职业概述 (professional summary) → 技术技能
(technical skills) → 工作经历 (work experience) → 重点项目 (key projects) →
教育背景 (education) → 语言能力 (languages). Sections you leave empty are left out.

The layout follows a classic Chinese tech résumé: US Letter, navy headings with a
rule under each, "Role — Company" with the dates on the right and the team or
area in italics below, bullet points, ◆ before each project with its tech stack
in grey, education on one line, languages on one line ("中文：母语 ｜ 英语：专业工作级").
Skills lines are kept exactly as you type them; "Label: …" makes the label bold.
A real bold font is used when your computer has one (e.g. Microsoft YaHei Bold).

- **With AI on**, the AI writes the introduction and, for each job post, picks
  and orders what's most relevant and polishes the wording, in your message
  language (Chinese by default).
- **It never invents anything.** For every skill and bullet point the AI must
  quote the text on your Profile page it came from. Job Radar checks the quote,
  and checks that any number (300万 → 3M is fine; a new "12" is not) is in it.
  Anything that fails the check is replaced by your own wording. Employers,
  schools and dates always come straight from your profile, and the AI never
  sees your phone, email, birth date or gender.
- **No promotions, no bigger roles.** Job titles and names stay exactly as you
  wrote them; they're only translated when the résumé is in another language,
  and a translation can't add seniority (工程师 never becomes "Senior Engineer").
  A rewritten point can't claim more responsibility than yours does: "参与开发"
  may become "helped build", never "led"; "used Kafka" never becomes "designed
  the Kafka layer". Same for the summary.
- **Skill check (no AI).** Job Radar lists the skills a post names and sorts
  them into: in your skills / shown in your experience / missing. Dry-run and
  "applied" notices show the match (🔎 技能匹配 5/7（71%）) and the dry run lists
  what's missing; the Preview shows all three groups. Missing skills are never
  put on the résumé, and the AI is told so. If you do have one, add it to your
  profile.
- **Newest job first.** Work experience is sorted by start date (2021.07,
  2021年7月, Jul 2021 and 2021 all work). If a job has no start date, your order
  is kept.
- **Paper and length.** US Letter by default, as in the sample; or A4, or
  automatic (Letter for US/Canada jobs, A4 elsewhere). You're warned when a
  résumé runs past the page limit (2 by default). Both are under **Auto-apply →
  Your résumé**.
- **Without AI**, the résumé is your profile as you typed it, nicely laid out.
- **Preview** on the Profile page makes it now (paste a job post to see the
  tailored version). In dry run, every draft also comes with the résumé it
  would send, and **Applications** links to the résumé each application sent.
- **Fill in from an existing résumé:** the AI copies the facts from a PDF into
  the fields for you to check. Nothing is saved until you press Save.
- **Files:** `jobradar-data/resumes/<job>/陈美_简历.pdf` (`Mei_Chen_Resume.pdf`
  in English). Your profile itself is stored in `jobradar.db` (`profile` table),
  so it's in your backups. The generated PDFs aren't; they can be made again.
- **Fonts:** Chinese text needs a Chinese font *embedded* in the PDF, so it
  looks right on any phone. Job Radar finds one on your computer (Windows:
  Microsoft YaHei / SimHei / SimSun; macOS: STHeiti; Linux: WenQuanYi) and
  embeds only the characters used (a résumé is ~50–100 KB). If none is found,
  put a `.ttf`/`.ttc` file in `jobradar-data/fonts`, or set `resume_font_path`.
  Only TrueType fonts work (not CFF-based `.otf` such as Noto Sans CJK).
- If a résumé can't be made (for example, no font), your uploaded PDF is sent
  instead; with neither, nothing is sent.

```bash
python3 -m jobradar.cli resume                       # your general résumé
python3 -m jobradar.cli resume --posting job.txt -o tailored.pdf
```

### Message language: Chinese by default

Applications, replies the AI writes, and the notices in your bot chat are in
**Chinese (简体中文)** unless you change it (dashboard: **Auto-apply → Message
language**):

```toml
message_language = "zh"     # "zh" (default) | "en" | "auto" = the job post's language
```

With `zh` the AI writes in Chinese even for English postings, keeping technical
terms (Go, Kubernetes…) as they are. Without AI, `apply_template_zh` /
`apply_email_subject_zh` are used for Chinese and `apply_template` /
`apply_email_subject` for English. Both are editable in the dashboard.

### Dry run first, then go live

```toml
apply_mode = "dry_run"
```

In dry run, everything happens (gates, AI scoring, drafting, the daily cap)
except contacting anyone. Each draft shows up in your bot chat as
**📝 演练 — 将会投递（匹配度 82/100）** (English: *📝 DRY RUN — would apply*) with the recipient, the reason, and the
full text. Run it for a few days and tune `apply_min_score`, `apply_keywords`
and `apply_exclude_keywords` until you'd be happy to send every draft. Then:

```toml
apply_mode = "auto"
```

Each real application is followed by **✅ 已投递，通过 Telegram @…** (*Applied via Telegram*) in your bot
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

### Conversations with representatives (Messages tab)

Every application that's actually sent opens a **conversation**, one per
representative (Telegram account or email address), so everything you've said
to one recruiter is in one thread, even across several jobs.

- **It's your real Telegram chat.** Applications are sent from your own account,
  so the whole history lives in Telegram, and you can carry on from your phone
  as usual. Job Radar *mirrors* each chat: every check (and every 5 minutes
  while the dashboard is open, or **Check for replies**) it reads the new
  messages in those chats, both their replies and anything you sent from your
  phone. It only reads chats with people it applied to, never your other chats.
- **New replies** show a badge on **Messages** and a 💬 notice in your bot chat
  (*“Anna Li 回复了你（后端工程师）…”*).
- **Reply from the dashboard.** Type in the box under a thread (Ctrl+Enter
  sends). Telegram replies go from your account, so they also appear in
  Telegram. Email replies go by SMTP with `Re: <subject>`.
- **Stored** in `jobradar.db`: the `conversations` table (one row per
  representative) and `messages` (each message, in or out, with the job it was
  about). The text of every application is also kept in `applications.message`,
  including dry-run drafts, and is shown under **Applications**.
- **Email replies aren't read yet.** That would need IMAP; read them in your
  mailbox. Your sent email (and dashboard replies) do appear in the thread.

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
runner and uploads `jobradar-windows` (the `.exe` + `jobradar-data/config.toml`) as a
downloadable artifact on every push, or from the Actions tab via "Run workflow".

Double-click the `.exe` and the dashboard opens in your browser, where you
set everything up. Settings, history and your résumé are stored in the
`jobradar-data` folder beside the executable. It holds your tokens, so keep it
private. To run headless instead (for example on a server), use
`jobradar.exe serve` with a hand-written `jobradar-data/config.toml`.

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
   **Jobs that don't say where they hire** are never silently dropped. By
   default (`unknown_location = "review"`) Job Radar asks you. Once per check,
   one Telegram message lists them with a ✅ / ❌ button pair each: ✅ sends you
   the job, and queues it for auto-apply if that's on; ❌ skips it for good.
   The dashboard's **Review** tab offers the same choice and shows the post.
   Unanswered jobs expire after `review_expiry_days` (14). If you keep skipping
   one source's jobs, the Review tab suggests switching it off. Prefer not to
   be asked? Set `unknown_location = "send"` to get them marked "📍 Location:
   not stated", or `"drop"` to drop them. On-site and hybrid jobs whose place
   isn't recognised are dropped, since an office is somewhere specific.
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
  state.py           the SQLite database (jobradar.db): sent jobs, sources, history,
                     applications, conversations and messages
  classifier.py      recall-first "is this a job posting?"
  extraction.py      title / remote / salary / contact / apply-url
  filters.py         keyword + remote
  dedup.py           near-match duplicate detection
  pipeline.py        the whole run: collect → filter → dedup → send
  service.py         wiring + the serve loop
  cli.py             entry point
  collectors/        one class per source type (Tier A + Tier B)
  delivery/          digest formatting + Telegram sender
  resume/            résumés from your profile: profile rules, content (AI checks),
                     fonts (find + subset TrueType), pdf (writer), render (layout), builder
  apply/             auto-apply: contacts, engine (gates/caps/queue), senders,
                     conversations (mirror + reply), language (zh/en)
  web/               the dashboard: app.py (server + API), index.html, configfile.py
tests/               python -m unittest discover -s tests
```

## Notes

- Non-commercial use only (a condition of Reddit's free tier).
- Tier B scraping conflicts with some sites' terms; the accepted consequence is
  losing access to those sites, which degrades gracefully — Tier A keeps working.
- The `contact` field can be personal data; it's kept only in the posting text
  and never written to logs.
