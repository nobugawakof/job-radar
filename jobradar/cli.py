"""Command-line entry point.

    jobradar init                 # create DB, owner, seed sources from config
    jobradar add-member "Alice"   # add a member, print their tokens
    jobradar run [--catchup]      # one collection run + delivery
    jobradar serve                # daemon: scheduler + Telegram bot + dashboard
    jobradar web                  # dashboard only
    jobradar sources              # list sources
    jobradar enable/disable NAME  # toggle a source (SR-1)
    jobradar export USER_ID       # DR-5 export
    jobradar backup               # NFR-7 manual backup
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from .config import load
from .service import Service


def _service(args: argparse.Namespace) -> Service:
    cfg = load(args.config)
    return Service(cfg, config_path=args.config)


def cmd_init(args: argparse.Namespace) -> int:
    svc = _service(args)
    owner_id = svc.init(owner_name=args.owner)
    owner = svc.store.get_user(owner_id)
    n = svc.seed_sources()
    print(f"Initialised. Owner '{owner['name']}' id={owner_id}")
    print(f"  Dashboard token: {owner['dashboard_token']}")
    print(f"  Telegram link code: {owner['telegram_link_code']}")
    print(f"  Seeded {n} source(s) from config.")
    svc.close()
    return 0


def cmd_add_member(args: argparse.Namespace) -> int:
    svc = _service(args)
    svc.init(owner_name=args.owner)
    info = svc.add_member(args.name, countries=args.country or [])
    print(json.dumps(info, indent=2))
    svc.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    svc = _service(args)
    svc.init(owner_name=args.owner)
    if args.catchup:
        summary = svc.scheduler.startup() or svc.scheduler.run_once("scheduled")
    else:
        summary = svc.run_and_deliver("manual")
    print(f"Run #{summary.run_id} ({summary.trigger}): "
          f"{summary.items_fetched} fetched, {summary.postings_detected} postings, "
          f"{summary.duplicates_merged} duplicates merged.")
    for a in summary.alerts:
        print(f"  ALERT: {a}")
    svc.close()
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    svc = _service(args)
    for s in svc.store.list_sources():
        state = "on" if s["enabled"] else "off"
        blocked = " (blocked)" if s["blocked"] else ""
        print(f"  {s['name']:<20} {s['type']:<10} tier {s['tier']}  {state}{blocked}  "
              f"fails={s['consecutive_failures']}  last={s['last_success_at'] or '—'}")
    svc.close()
    return 0


def cmd_toggle(enable: bool):
    def _run(args: argparse.Namespace) -> int:
        svc = _service(args)
        svc.store.set_source_enabled(args.name, enable)
        print(f"Source '{args.name}' {'enabled' if enable else 'disabled'}.")
        svc.close()
        return 0

    return _run


def cmd_export(args: argparse.Namespace) -> int:
    svc = _service(args)
    print(json.dumps(svc.store.export_user(args.user_id), indent=2, default=str))
    svc.close()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    svc = _service(args)
    from .db import utcnow

    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest = f"{svc.config.backup_dir}/jobradar-{stamp}.db"
    svc.db.backup_to(dest)
    print(f"Backed up to {dest}")
    svc.close()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    svc = _service(args)
    svc.init(owner_name=args.owner)
    from .web.app import build_server

    server = build_server(svc.build_web_context())
    host, port = server.server_address[0], server.server_address[1]
    print(f"Dashboard on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        svc.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Full daemon: scheduler + bot polling + dashboard."""
    svc = _service(args)
    svc.init(owner_name=args.owner)
    from .web.app import build_server

    server = build_server(svc.build_web_context())
    web_thread = threading.Thread(target=server.serve_forever, daemon=True)
    web_thread.start()
    host, port = server.server_address
    print(f"Dashboard on http://{host}:{port}")

    # Bot polling loop in its own thread (if a token is configured).
    stop = threading.Event()

    def bot_loop() -> None:
        while not stop.is_set():
            try:
                svc.poll_bot()
            except Exception:  # noqa: BLE001
                logging.getLogger("jobradar").exception("bot poll failed")
            stop.wait(5)

    if svc._transport:
        threading.Thread(target=bot_loop, daemon=True).start()

    # Startup catch-up (FR-4), then the scheduled loop.
    try:
        svc.scheduler.startup()
        svc.deliver()
        while True:
            wait = svc.scheduler.seconds_until_next()
            if wait <= 0:
                svc.run_and_deliver("scheduled")
            else:
                time.sleep(min(wait, 30))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        svc.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobradar", description="Social Job Radar (Phase 1)")
    p.add_argument("--config", default="config.toml", help="path to config TOML")
    p.add_argument("--owner", default="owner", help="owner display name for init")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create DB, owner, seed sources").set_defaults(func=cmd_init)

    am = sub.add_parser("add-member", help="add a member")
    am.add_argument("name")
    am.add_argument("--country", action="append", help="eligible country (repeatable)")
    am.set_defaults(func=cmd_add_member)

    r = sub.add_parser("run", help="one collection run + delivery")
    r.add_argument("--catchup", action="store_true", help="run startup catch-up logic")
    r.set_defaults(func=cmd_run)

    sub.add_parser("sources", help="list sources").set_defaults(func=cmd_sources)

    en = sub.add_parser("enable", help="enable a source")
    en.add_argument("name")
    en.set_defaults(func=cmd_toggle(True))
    di = sub.add_parser("disable", help="disable a source")
    di.add_argument("name")
    di.set_defaults(func=cmd_toggle(False))

    ex = sub.add_parser("export", help="export a user's data (DR-5)")
    ex.add_argument("user_id")
    ex.set_defaults(func=cmd_export)

    sub.add_parser("backup", help="back up the database (NFR-7)").set_defaults(func=cmd_backup)
    sub.add_parser("web", help="run the dashboard only").set_defaults(func=cmd_web)
    sub.add_parser("serve", help="run the full daemon").set_defaults(func=cmd_serve)
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
