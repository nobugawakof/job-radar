"""Where Job Radar keeps its files: one ``jobradar-data`` folder.

Everything the program creates lives in a single folder beside it (beside the
.exe, or in the folder you start it from):

    jobradar-data/
      config.toml             settings and keys (kept out of the database, SRS DR-6)
      jobradar.db             history: sent jobs, applications, check history…
      jobradar-user.session   your Telegram login, if you logged in (keep private)
      resume.pdf / .txt       your résumé, if uploaded
      jobradar-web.token      the dashboard's session-cookie secret

Older versions kept these files loose beside the program. The first time the
default location is used, they're moved into the folder — all or nothing: if
any move fails, the ones already done are put back. An explicit ``--config``
path is always used as given and never moved.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

log = logging.getLogger("jobradar.datadir")

DATA_DIR_NAME = "jobradar-data"
CONFIG_NAME = "config.toml"

# Files older versions created beside the program, moved with config.toml.
_KNOWN_FILES = [
    "config.toml.bak", "jobradar.db", "jobradar.db-wal", "jobradar.db-shm",
    "jobradar-state.json", "jobradar-state.json.migrated", "jobradar-user.session",
    "jobradar-user.session-journal", "jobradar-web.token", "resume.pdf", "resume.txt",
]
# Config keys that may name a file relative to the config's folder.
_PATH_KEYS = ["state_path", "telegram_session_path", "resume_pdf_path", "resume_text_path"]


def _referenced_files(config: Path) -> list[str]:
    """Plain file names (relative, no folders) the old config points at."""
    try:
        data = tomllib.loads(config.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    names = []
    for key in _PATH_KEYS:
        v = data.get(key)
        if isinstance(v, str) and v and not Path(v).is_absolute() and Path(v).name == v:
            names.append(v)
            if key == "telegram_session_path" and not v.endswith(".session"):
                names.append(v + ".session")
            if key == "state_path":
                names += [v + "-wal", v + "-shm"]
                if v.endswith(".json"):  # the SQLite file that replaced it
                    db = v[:-5] + ".db"
                    names += [db, db + "-wal", db + "-shm"]
    return names


def _in_use(base: Path) -> bool:
    """Is another Job Radar running from the old layout right now? Read-only:
    opening the database normally could "repair" a damaged file, which a mere
    check must never do."""
    import json
    import socket
    import sqlite3

    from .state import _pid_alive

    db = base / "jobradar.db"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'run_lock'").fetchone()
        holder = json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return False  # not a database, or no lock table: nobody is using it
    finally:
        con.close()
    return bool(holder and holder.get("host") == socket.gethostname()
                and holder.get("pid") != os.getpid() and _pid_alive(int(holder.get("pid") or 0)))


def migrate_loose_files(base: Path) -> bool:
    """Move an old loose layout into ``jobradar-data``. True if moved."""
    old_config = base / CONFIG_NAME
    data = base / DATA_DIR_NAME
    if not old_config.exists() or (data / CONFIG_NAME).exists():
        return False
    if _in_use(base):
        log.warning("not moving files into %s: another Job Radar is running here", DATA_DIR_NAME)
        return False
    names = list(dict.fromkeys(_KNOWN_FILES + _referenced_files(old_config)))
    moves = [(base / n, data / n) for n in names if (base / n).is_file()]
    moves.append((old_config, data / CONFIG_NAME))  # last: it marks the move as done
    clashes = [dst.name for _, dst in moves if dst.exists()]
    if clashes:
        log.warning("not moving files into %s: it already has %s", DATA_DIR_NAME, ", ".join(clashes))
        return False
    data.mkdir(exist_ok=True)
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            os.replace(src, dst)
            done.append((src, dst))
    except OSError as e:
        for src, dst in reversed(done):  # put everything back
            try:
                os.replace(dst, src)
            except OSError:
                log.error("couldn't move %s back to %s", dst, src)
        log.warning("couldn't move files into %s (%s); keeping the old layout", DATA_DIR_NAME, e)
        return False
    log.info("moved %d file(s) into %s/", len(done), DATA_DIR_NAME)
    return True


def default_config_path(base: Path) -> Path:
    """The config file to use when none is given: jobradar-data/config.toml,
    after moving an older loose layout into it."""
    migrate_loose_files(base)
    data = base / DATA_DIR_NAME
    data.mkdir(exist_ok=True)
    return data / CONFIG_NAME
