"""Command-line entry point (Telegram-only build).

    jobradar run          # one collection cycle → Telegram digest
    jobradar serve        # loop forever at the configured interval
    jobradar sources      # list configured sources
    jobradar test-telegram  # send a test message to the configured chat
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load
from .service import Service


def _service(args: argparse.Namespace) -> Service:
    return Service(load(args.config), config_path=args.config)


def cmd_run(args: argparse.Namespace) -> int:
    svc = _service(args)
    s = svc.run("manual")
    print(f"Fetched {s.items_fetched}, detected {s.postings_detected} posting(s), "
          f"merged {s.duplicates_merged} dup(s), skipped {s.already_sent_skipped} already-sent, "
          f"sent {len(s.sent)}.")
    if not svc.transport:
        print("  (no Telegram token configured — nothing was sent; set "
              "JOBRADAR_TELEGRAM_BOT_TOKEN and JOBRADAR_TELEGRAM_CHAT_ID)")
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
    p.add_argument("--config", default="config.toml", help="path to config TOML")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="one collection cycle + Telegram digest").set_defaults(func=cmd_run)
    sub.add_parser("serve", help="loop at the configured interval").set_defaults(func=cmd_serve)
    sub.add_parser("sources", help="list configured sources").set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
