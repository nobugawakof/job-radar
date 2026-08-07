"""Command-line entry point (Telegram-only build).

    jobradar run          # one collection cycle → Telegram digest
    jobradar serve        # loop forever at the configured interval
    jobradar sources      # list configured sources
    jobradar test-telegram  # send a test message to the configured chat
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import load
from .service import Service


def _frozen() -> bool:
    """True when running as a PyInstaller-built executable."""
    return getattr(sys, "frozen", False)


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
    return str(base_dir() / "config.toml")


def _service(args: argparse.Namespace) -> Service:
    return Service(load(args.config), config_path=args.config)


def cmd_run(args: argparse.Namespace) -> int:
    svc = _service(args)
    s = svc.run("manual")
    # Per-source breakdown, so it's clear where postings come from (or don't).
    print(f"Fetched {s.items_fetched} item(s) from {len(s.per_source)} source(s):")
    for name, meta in s.per_source.items():
        if meta.get("status") == "error":
            print(f"  - {name}: ERROR — {meta.get('error')}")
        elif meta.get("status") == "blocked":
            print(f"  - {name}: BLOCKED — {meta.get('error')}")
        else:
            print(f"  - {name}: {meta.get('items', 0)} fetched")
    if s.skipped_old:
        print(f"Skipped {s.skipped_old} posting(s) older than "
              f"{svc.config.max_posting_age_days} days.")
    # Filter funnel, so it's clear why postings were or weren't sent.
    print(f"Detected {s.postings_detected} posting(s) → {s.passed_filter} passed filters "
          f"(rejected: {s.rejected_keyword} keyword, {s.rejected_remote} remote, "
          f"{s.rejected_region} region, {s.rejected_salary} salary).")
    tail = f", held {s.held_back} for later runs" if s.held_back else ""
    print(f"After dedup ({s.duplicates_merged} merged) and already-sent "
          f"({s.already_sent_skipped}): {len(s.sent)} to send → {s.delivered} delivered{tail}.")
    if not svc.transport:
        print("  (no Telegram token configured — nothing was sent; set "
              "telegram_bot_token / telegram_chat_id in config.toml)")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobradar", description="Social Job Radar (Telegram-only)")
    p.add_argument("--config", default=default_config_path(), help="path to config TOML")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="one collection cycle + Telegram digest").set_defaults(func=cmd_run)
    sub.add_parser("serve", help="loop at the configured interval").set_defaults(func=cmd_serve)
    sub.add_parser("sources", help="list configured sources").set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Double-clicking the .exe passes no arguments — default to the daemon so it
    # just starts sending jobs, rather than printing an argparse error.
    if not argv and _frozen():
        argv = ["serve"]

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
