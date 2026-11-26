"""Command-line entry point (Telegram-only build).

    jobradar web          # open the dashboard (set up + run from your browser)
    jobradar run          # one collection cycle → Telegram digest
    jobradar serve        # loop forever at the configured interval
    jobradar sources      # list configured sources
    jobradar test-telegram  # send a test message to the configured chat
    jobradar telegram-login # log your own Telegram account in (for auto-apply)
    jobradar test-apply     # draft an application for a sample posting
    jobradar resume         # make your résumé PDF from your profile
    jobradar applications   # list recent auto-apply results
"""

from __future__ import annotations

import argparse
import logging
import time
import os
import sys
from pathlib import Path

from .config import load
from .service import Service


def _frozen() -> bool:
    """True when running as a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


def _disable_console_quick_edit() -> None:
    """Stop a stray mouse click from freezing the daemon (Windows only).

    Windows consoles default to "QuickEdit mode": clicking in the window puts it
    into Mark/Select mode (the title bar shows "Select …") and — crucially —
    *suspends the running program* until a key is pressed. For a long-running
    daemon that looks exactly like a hang: no output, nothing sent, for as long
    as the selection is held. We turn QuickEdit off so a click can't pause it.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_EXTENDED_FLAGS = 0x0080
        ENABLE_QUICK_EDIT_MODE = 0x0040
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:  # noqa: BLE001 - never let this cosmetic tweak break startup
        pass


def base_dir() -> Path:
    """Directory to resolve config/state against.

    For a packaged .exe this is the folder the executable sits in, so a user can
    drop config.toml next to jobradar.exe and double-click it. Otherwise it's the
    current working directory.
    """
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_config_path() -> str:
    """jobradar-data/config.toml beside the program (an older loose layout is
    moved there first; see jobradar/datadir.py)."""
    from .datadir import default_config_path as data_config

    return str(data_config(base_dir()))


def _service(args: argparse.Namespace) -> Service:
    return Service(load(args.config), config_path=args.config)


def cmd_run(args: argparse.Namespace) -> int:
    from .service import AlreadyRunning

    svc = _service(args)
    try:
        s = svc.run("manual")
    except AlreadyRunning as e:
        print(f"Not checking now: {e}. Try again when it has finished.")
        return 1
    # Per-source breakdown, so it's clear where postings come from (or don't).
    print(f"Fetched {s.items_fetched} item(s) from {len(s.per_source)} source(s):")
    for name, meta in s.per_source.items():
        if meta.get("status") == "error":
            print(f"  - {name}: ERROR — {meta.get('error')}")
        elif meta.get("status") == "blocked":
            print(f"  - {name}: BLOCKED — {meta.get('error')}")
        else:
            # Show the funnel per source so it's obvious where postings are lost:
            # how many were fetched, passed all filters, and were actually sent,
            # plus the main reasons the rest were dropped.
            drops = []
            for stage, label in (("not_job", "not-a-job"), ("old", "too-old"),
                                 ("unreadable", "unreadable"), ("rej_excluded", "excluded"),
                                 ("rej_keyword", "keyword"), ("rej_remote", "remote"),
                                 ("rej_region", "region"), ("rej_salary", "salary")):
                if meta.get(stage):
                    drops.append(f"{meta[stage]} {label}")
            detail = f" [dropped: {', '.join(drops)}]" if drops else ""
            print(f"  - {name}: {meta.get('items', 0)} fetched, "
                  f"{meta.get('passed', 0)} passed, {meta.get('sent', 0)} sent{detail}")
    if s.skipped_old:
        print(f"Skipped {s.skipped_old} posting(s) older than "
              f"{svc.config.max_posting_age_days} days.")
    # Filter funnel, so it's clear why postings were or weren't sent.
    print(f"Detected {s.postings_detected} posting(s) → {s.passed_filter} passed filters "
          f"(rejected: {s.rejected_keyword} keyword, {s.rejected_excluded} excluded, "
          f"{s.rejected_remote} remote, {s.rejected_region} region, {s.rejected_salary} salary).")
    ai_note = f", {s.rejected_ai} vetoed by AI" if s.rejected_ai else ""
    tail = f", held {s.held_back} for later runs" if s.held_back else ""
    reposts = f", incl. {s.reposts_skipped} reworded repost(s)" if s.reposts_skipped else ""
    print(f"After dedup ({s.duplicates_merged} merged) and already-sent "
          f"({s.already_sent_skipped}{reposts}){ai_note}: {len(s.sent)} to send → "
          f"{s.delivered} delivered{tail}.")
    if not svc.transport:
        print("  (no Telegram token configured — nothing was sent; set "
              "telegram_bot_token / telegram_chat_id in config.toml)")
    if s.queued_review:
        print(f"{s.queued_review} job(s) don't say where they hire — waiting for your review "
              "(Telegram buttons, or the dashboard's Review tab).")
    if s.apply is not None:
        r = s.apply
        did = f"{r.previewed} previewed (dry run)" if r.mode == "dry_run" else f"{r.sent} sent"
        print(f"Auto-apply [{r.mode}]: {r.queued} newly queued → {did}, "
              f"{r.skipped_fit} low fit, {r.skipped_cooldown} contact cooldown, "
              f"{r.failed} failed, {r.expired} expired; {r.waiting} waiting in queue.")
    for a in s.alerts:
        print(f"  ALERT: {a}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    svc = _service(args)
    try:
        svc.run_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    svc = _service(args)
    if not svc.source_defs:
        print("No sources configured. Add [[sources]] blocks to your config.")
        return 0
    for s in svc.source_defs:
        name = s["name"]
        disabled = " (disabled by failures)" if svc.state.is_disabled(name) else ""
        enabled = "on" if s.get("enabled", True) else "off"
        last = svc.state.source(name).get("last_success") or "—"
        print(f"  {name:<22} {s['type']:<10} tier {s.get('tier','A')}  {enabled}{disabled}  last={last}")
    return 0


def cmd_test_telegram(args: argparse.Namespace) -> int:
    svc = _service(args)
    if not (svc.transport and svc.config.telegram_chat_id):
        print("Telegram not configured. Set JOBRADAR_TELEGRAM_BOT_TOKEN and "
              "JOBRADAR_TELEGRAM_CHAT_ID (or put them in the config).")
        return 1
    svc.transport.send_message(svc.config.telegram_chat_id, "✅ Job Radar test message.")
    print("Sent a test message.")
    return 0


def cmd_telegram_login(args: argparse.Namespace) -> int:
    """Interactive one-time login of YOUR Telegram account for auto-apply."""
    from .apply.engine import _resolve

    cfg = load(args.config)
    if not (cfg.telegram_api_id and cfg.telegram_api_hash):
        print("Set telegram_api_id and telegram_api_hash in config.toml first "
              "(get them at https://my.telegram.org → API development tools).")
        return 1
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print("This needs Telethon:  pip install telethon")
        return 1
    session = _resolve(Path(args.config).resolve().parent, cfg.telegram_session_path)
    client = TelegramClient(str(session), int(cfg.telegram_api_id), cfg.telegram_api_hash)
    # Prompts for the login code Telegram sends you (and your 2FA password, if set).
    client.start(phone=cfg.telegram_phone or (lambda: input("Phone number (+country code): ")))
    me = client.get_me()
    client.disconnect()
    handle = f"@{me.username}" if getattr(me, "username", None) else (me.first_name or "your account")
    print(f"Logged in as {handle}. Session saved to {session}")
    print("Keep that file private — it gives full access to your Telegram account.")
    return 0


_SAMPLE_POSTING = """We're hiring: Senior Backend Engineer (Python) — fully remote, worldwide
Acme Labs builds data infrastructure for fintech teams.
You will: design and run Python/Go services on AWS, own APIs end to end,
and mentor two junior engineers.
Requirements: 5+ years backend, Python, PostgreSQL, Docker/Kubernetes, AWS.
Salary: $90k-$130k. Apply: Telegram @acme_hiring or jobs@acme.example"""


def cmd_resume(args: argparse.Namespace) -> int:
    """Make your résumé PDF from your profile, optionally tailored to a posting."""
    from .resume.builder import make_builder
    from .resume.profile import ProfileError, validate

    svc = _service(args)
    profile = svc.state.get_profile()
    try:
        validate(profile)
    except ProfileError as e:
        print(f"Your profile isn't complete: {e} Fill it in on the dashboard's Profile page.")
        return 1
    builder = make_builder(svc.config, svc.base_dir, profile)
    text = Path(args.posting).read_text("utf-8") if args.posting else ""
    try:
        path = builder.build(text)
    except Exception as e:  # noqa: BLE001
        print(f"Couldn't make the résumé: {e}")
        return 1
    if args.output:
        out = Path(args.output)
        out.write_bytes(path.read_bytes())
        path = out
    how = "tailored by AI" if builder.tailor and text else "from your profile"
    info = builder.last
    print(f"Résumé ({how}): {path}")
    print(f"{info.pages} page(s), {info.paper.upper() if info.paper == 'a4' else 'Letter'}"
          + (f" — over the {info.max_pages}-page limit" if info.too_long else ""))
    r = info.skills
    if r.skills:
        print(f"Skills the post names: {r.matched}/{len(r.skills)} in your profile ({r.percent}%)")
        if r.existing:
            print("  listed in your skills: " + ", ".join(r.existing))
        if r.supported:
            print("  shown in your experience: " + ", ".join(r.supported))
        if r.gap:
            print("  missing (not put on the résumé): " + ", ".join(r.gap))
    return 0


def cmd_test_apply(args: argparse.Namespace) -> int:
    """Draft an application for a sample (or given) posting; optionally send it
    to YOURSELF (Telegram Saved Messages / your own email) to check the wiring."""
    from .apply.engine import build_applier, build_senders

    svc = _service(args)
    cfg = svc.config
    text = Path(args.posting).read_text("utf-8") if args.posting else _SAMPLE_POSTING
    applier = build_applier(cfg, svc.state, base_dir=svc.base_dir)
    from .extraction import extract_title

    title = extract_title(text)
    draft = applier.drafter({"title": title, "description": text})
    if draft is None:
        print("Could not draft an application (AI failed and no apply_keywords set for the "
              "template fallback). Check use_ai / API key / resume paths.")
        return 1
    print(f"Drafted by: {draft.source}" + (f"  fit {draft.fit_score}/100 (threshold "
          f"{cfg.apply_min_score}) — {draft.fit_reason}" if draft.fit_score is not None else ""))
    print("\n--- Telegram message ---\n" + draft.message)
    print(f"\n--- Email ---\nSubject: {draft.email_subject}\n\n{draft.email_body}")
    pdf, _ = applier._resume_for("test-apply", {"title": title, "description": text})
    print(f"\nRésumé attachment: {pdf if pdf else 'NONE — fill in your profile or upload a PDF'}")
    if not args.send:
        print("\n(Not sent. Add --send to deliver this to yourself: Telegram Saved Messages "
              "and your own email.)")
        return 0
    attachment = pdf
    rc = 0
    for ch, sender in build_senders(cfg, svc.base_dir).items():
        to = "me" if ch == "telegram" else (cfg.smtp_from or cfg.smtp_user)
        body = draft.message if ch == "telegram" else draft.email_body
        try:
            sender.send(to, message=body, subject=draft.email_subject or "Test application",
                        attachment=attachment)
            print(f"Sent test via {ch} to {to}.")
        except Exception as e:  # noqa: BLE001
            print(f"{ch}: FAILED — {e}")
            rc = 1
        finally:
            sender.close()
    return rc


def cmd_applications(args: argparse.Namespace) -> int:
    svc = _service(args)
    recs = svc.state.list_applications(args.limit)
    waiting = svc.state.queue_len()
    if not recs and not waiting:
        print("No applications yet.")
        return 0
    for r in recs:
        score = f"fit {r['score']:>3}" if r.get("score") is not None else "fit   —"
        to = f"{r.get('channel') or ''} {r.get('contact') or ''}".strip() or "—"
        note = r.get("error") or r.get("reason") or ""
        print(f"  {r.get('at', '')[:16]}  {r.get('mode', ''):<7} {r.get('status', ''):<16} "
              f"{score}  {to:<32} {r.get('title', '')[:60]}" + (f"\n{'':<20}↳ {note[:110]}" if note else ""))
    if waiting:
        print(f"\n{waiting} posting(s) waiting in the apply queue.")
    for ch, until in svc.state.paused_channels().items():
        print(f"  {ch} paused until {until[:16]}Z")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import make_backup

    data = make_backup(args.config, include_login=args.with_login)
    out = Path(args.output or time.strftime("jobradar-backup-%Y%m%d-%H%M.zip"))
    out.write_bytes(data)
    print(f"Saved {out} ({len(data) // 1024} KB){' — includes your Telegram login' if args.with_login else ''}.")
    print("It contains your keys: keep it private.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .backup import BackupError, data_files, read_backup, restore_backup
    from .state import State

    data = Path(args.file).read_bytes()
    try:
        manifest, contents = read_backup(data)
    except BackupError as e:
        print(f"Can't restore: {e}")
        return 1
    print(f"Backup from {manifest.get('created', '?')[:16]}: {', '.join(sorted(contents))}.")
    if not args.yes and input("Replace your current settings and history with it? [y/N] ").strip().lower() != "y":
        print("Nothing changed.")
        return 1
    cfg_path = Path(args.config)
    db = data_files(cfg_path).get("database") if cfg_path.exists() else None
    if db is not None and db.exists():
        st = State(db)
        busy = not st.acquire_run_lock("restore")
        st.close()
        if busy:
            print("Another Job Radar is running a check. Close it first, then restore.")
            return 1
    result = restore_backup(data, cfg_path)
    print(f"Restored: {', '.join(result['restored'])}.")
    if result["previous_files"]:
        print(f"Your previous files were moved to {result['previous_files']}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web.app import main as web_main

    return web_main(args.config, host=args.host, port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobradar", description="Social Job Radar (Telegram-only)")
    p.add_argument("--config", default=None,
                   help="path to config TOML (default: jobradar-data/config.toml beside the program)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    web = sub.add_parser("web", help="open the dashboard in your browser (recommended)")
    web.add_argument("--host", default="127.0.0.1",
                     help="interface to listen on (default: this computer only)")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    web.set_defaults(func=cmd_web)
    sub.add_parser("run", help="one collection cycle + Telegram digest").set_defaults(func=cmd_run)
    bk = sub.add_parser("backup", help="save settings, history and résumé to one .zip")
    bk.add_argument("-o", "--output", help="file to write (default: jobradar-backup-<date>.zip)")
    bk.add_argument("--with-login", action="store_true",
                    help="also include your Telegram login (full access to your account)")
    bk.set_defaults(func=cmd_backup)
    rs = sub.add_parser("restore", help="restore a backup .zip (current files are kept aside)")
    rs.add_argument("file")
    rs.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    rs.set_defaults(func=cmd_restore)
    sub.add_parser("serve", help="loop at the configured interval").set_defaults(func=cmd_serve)
    sub.add_parser("sources", help="list configured sources").set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)
    sub.add_parser("telegram-login", help="log your own Telegram account in (auto-apply)"
                   ).set_defaults(func=cmd_telegram_login)
    rs = sub.add_parser("resume", help="make your résumé PDF from your profile")
    rs.add_argument("--posting", help="a text file with a job post to tailor it to")
    rs.add_argument("-o", "--output", help="where to save the PDF")
    rs.set_defaults(func=cmd_resume)
    ta = sub.add_parser("test-apply", help="draft an application for a sample posting")
    ta.add_argument("--posting", help="text file with a real posting to draft for")
    ta.add_argument("--send", action="store_true",
                    help="also send it to yourself (Telegram Saved Messages + your email)")
    ta.set_defaults(func=cmd_test_apply)
    ap = sub.add_parser("applications", help="list recent auto-apply results")
    ap.add_argument("--limit", type=int, default=30)
    ap.set_defaults(func=cmd_applications)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Double-clicking the .exe passes no arguments — open the dashboard (which
    # also runs the schedule) rather than printing an argparse error.
    if not argv and _frozen():
        argv = ["web"]

    _disable_console_quick_edit()  # a stray click must not freeze the daemon
    args = build_parser().parse_args(argv)
    if args.config is None:
        args.config = default_config_path()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("telethon").setLevel(logging.WARNING)  # connection chatter
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001 - keep the console window open on error
        logging.getLogger("jobradar").error("%s", e)
        if _frozen():
            print(f"\nError: {e}")
            input("Press Enter to close...")
        return 1


if __name__ == "__main__":
    sys.exit(main())
