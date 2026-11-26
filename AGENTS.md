# Job Radar: handbook for coding agents

Read this file first. It holds what earlier sessions learned and decided, so
work can continue after a context reset or with a different agent. When you
learn something that the next agent will need, add it here or to `docs/dev/`
in the same commit.

More detail:
- `docs/dev/architecture.md`: modules, data flow, database tables.
- `docs/dev/decisions.md`: why things are the way they are. Check it before
  "fixing" something that looks odd.
- `docs/dev/status.md`: what's done and what's still open (the audit list).
- `.claude/skills/`: step-by-step recipes for recurring tasks. Any agent can
  read them as plain Markdown.
- `docs/SRS.md`: the original requirements spec (Draft 0.1, partly outdated; see status.md E1).
- `README.md`: the user guide. Keep it in sync with behaviour changes.

## What it is

A personal job-hunting bot. It collects job posts from Telegram channels, RSS
feeds, HN, Reddit, Bluesky, V2EX and other sites, then filters them by keyword,
remote, region and salary. It drops duplicates and reposts and sends each new
match to the owner's Telegram bot chat. Optional extras:
- **AI:** Gemini or Claude, for extraction and fit scoring.
- **Auto-apply:** sends an application and a résumé PDF from the owner's own
  Telegram account or by email.
- **Generated résumés:** the owner fills in a Profile (name, job title, education
  required); a PDF résumé is made per application, tailored to the job by AI.
  The PDF writer is in-house (stdlib), embedding a subset TrueType font for Chinese.
- **Conversations:** one thread per recruiter, mirrored from that Telegram account.
- **Dashboard:** a local web page for all setup; optionally reachable from
  the owner's other devices (home network or Tailscale), with a QR code.
- **Accounts:** open sign-up; the administrator is `kof311` (user id 1, owns
  all pre-account data). Users get their own profile, Telegram login, alerts
  chat, applications and messages; settings stay admin-only.

## Hard rules

- **Standard library only** (Python ≥ 3.11). `dependencies = []` in
  `pyproject.toml`. The only optional packages are `telethon` (the user's own
  Telegram account) and `pyinstaller` (building the .exe). Import Telethon
  lazily, through `jobradar/tgclient.py` only.
- **Secrets never go in the database or logs.** Keys stay in `config.toml`
  (spec DR-6). Pass log text through `jobradar/redact.py`. Mask contacts in
  logs (`@h***na`, `j***@acme.io`).
- **Telethon goes through `tgclient.LoopThread`**, with a hard timeout on every
  call. Hold **`tgclient.ACCOUNT_LOCK`** whenever a client is open on the
  session file:
  - acquire it with a timeout;
  - on timeout, report "busy" instead of waiting;
  - never open the session file from two threads.
- **Database access goes through `jobradar/state.py`** (`State` methods), with
  one short transaction per operation. Personal data is per user:
  `state.for_user(uid)` gives that user's view (applications, queue, contacts,
  pauses, conversations, profile); the plain `State` is the administrator's
  (user 1). Never read another user's rows from a user-facing endpoint.
- **Every dashboard route has a role** in `web/app.py` `ROLES` (public / user
  / admin, default admin). User routes act only on `self.st` / `self.ucfg`
  (the caller's own data and settings). `kof311` can't be registered,
  restricted or deleted; the printed `?t=` link logs in the admin only from
  loopback. Schema changes follow
  `.claude/skills/schema-migration`.
- **Never send twice.**
  - Applications are committed the moment they're sent.
  - Each posting is applied to at most once, and each contact gets a 30-day cooldown.
  - Only one check runs at a time, across processes.
  - Don't weaken any of these.
- **Recipients come from the rules in `apply/contacts.py`, never from the AI.**
- **Résumés never contain invented facts.** The AI's tailored résumé is checked
  in `resume/content.py`: every skill and bullet must quote its source text from
  the profile, and numbers must appear in that source (unit conversions like
  300万 → 3M are allowed). Employers, schools and dates always come from the
  profile. Titles and names are kept verbatim in the same language, and a
  translation can't add seniority; rewritten bullets and the summary can't
  gain lead/own/design/manage/found verbs or drop "participated/参与"
  (`_label`, `_no_inflation`). Anything that fails falls back to the owner's
  own wording. Don't loosen these checks. The AI never receives phone, email, birth date or gender
  (`ai.resume_facts`).
- **Chinese is the default message language** (`message_language = "zh"`):
  applications, AI drafts and bot notices. Every user-facing phrase Job Radar
  writes to a recruiter or the bot chat goes through `apply/language.py`, in
  both `zh` and `en`. The dashboard UI itself is English.
- Code comments and docs stay in plain English. Don't put model names or
  session details in commits or code comments.

## Commands

```bash
python3 -m unittest discover -s tests          # full suite, about 3 s, must stay green
python3 -m unittest tests.test_messages -v     # one file
python3 -m jobradar.cli --config /tmp/x/jobradar-data/config.toml web --no-browser --port 8794
python3 -m jobradar.cli run                    # one check
python3 -m pyflakes jobradar tests             # lint (pip install pyflakes)
python3 build.py                               # standalone executable (needs pyinstaller)
```

The tests make no network calls. Each fake replaces a real outside service:
- `ai._post_json` for the AI;
- `tgclient.make_client` for Telethon, returning a FakeClient with async methods;
- `apply.engine.build_senders` for senders;
- HTTP collectors are fed canned responses.

Follow the patterns in `tests/test_messages.py` and `tests/test_web.py`. In
dashboard tests, start `web.make_server(cfg, port=0)` and send the cookie and the
`X-Requested-With: jobradar` header.

## Working with the owner

- Communicate in English, briefly. The owner reads in the Claude app.
- When several next steps are possible, the owner prefers an
  **AskUserQuestion selection window** (put the recommended option first).
  They pick items one by one from numbered lists (B1, S1, …). Keep that numbering.
- Develop on the session's assigned `claude/...` branch. Open a PR only when
  asked. After a PR is merged, restart the branch from `origin/main`.
- Before reporting done:
  - Tests pass.
  - For UI changes, run a browser check (`.claude/skills/verify-dashboard`).
  - The README has been updated.
  - Say plainly what couldn't be tested (see "Sandbox limits").

## Sandbox limits (cloud sessions)

- **Real services are blocked here:** Telegram (bot API and MTProto), the AI
  APIs, SMTP and most job sites. Features that use them are tested only with
  fakes, so always tell the owner which parts are unverified against real
  services (status.md, V1–V7).
- **Installing Telethon:** `pip install telethon` fails because the pyaes wheel
  won't build. Use `pip install --no-deps telethon rsa pyasn1`, then copy the
  `pyaes` package from its source tarball:
  `pip download pyaes --no-deps --no-binary :all:`, untar, copy `pyaes/` into site-packages.
- **A Telethon `connect()` against the blocked network hangs.** That's why
  every Telethon call has a timeout.
- **A safety filter can block shell heredocs that contain JS template literals
  (`${...}`).** Write the edit script to a scratch file with the Write tool,
  then run `python3 script.py ROOT`.
- Chromium and Playwright are preinstalled (`require('playwright')` from Node).
  Don't reinstall them.

## PDF and fonts

- `resume/pdf.py` writes PDFs by hand: Type0/CIDFontType2, Identity-H, a
  ToUnicode map (so text is selectable and parseable), with Helvetica as the
  fallback for Latin-only text. `resume/fonts.py` parses TTF/TTC, finds fonts
  per OS, and subsets by emptying unused glyphs (glyph ids stay the same).
- Only TrueType outlines (`glyf`) are supported; CFF `.otf` fonts are skipped.
- Bold: `fonts.find_bold` finds the bold sibling (msyh→msyhbd, arial→arialbd,
  DejaVuSans→DejaVuSans-Bold…) and embeds it as F2; otherwise bold is stroked.
  Italic is a skewed text matrix. The layout copies the owner's sample résumé
  (see decisions.md); compare visually after any change to `resume/render.py`.
- `soffice` (LibreOffice) is installed here and can turn a .docx into a PDF for
  side-by-side comparison: `soffice --headless --convert-to pdf --outdir DIR FILE`.
- To check a PDF here: `pdftotext file.pdf -`, `pdffonts file.pdf`,
  `pdftoppm -r 70 -png file.pdf out` (poppler is installed; then Read the PNG).
  Installing other PDF libraries with pip was refused in this sandbox.
- The sandbox has a Chinese font (WenQuanYi Zen Hei); tests that need one are
  skipped where none exists.

## Editing tips

- `jobradar/web/index.html` is one large file: CSS, HTML and vanilla JS, with
  no build step and a strict CSP (no inline handlers, no external scripts).
  - Class names are global. A generic name like `.top` collided with the header
    once, so prefix new classes (`.conv-top`).
  - Form fields bind to config keys with `data-k="<key>"`.
  - Long unbroken text (paths, tokens, URLs) must be able to wrap. `.mono`
    has `overflow-wrap: anywhere`; the long data-folder path once made the
    Overview tab too wide on phones.
- Edit scripts that replace exact strings should `assert s.count(old) == 1` first.
