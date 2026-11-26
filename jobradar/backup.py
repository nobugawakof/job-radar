"""Back up everything to one file, and restore it.

A backup is a .zip holding:

    jobradar-backup.json     what's inside, and when it was made
    config.toml              settings and keys
    jobradar.db              the database (a consistent snapshot, safe while running)
    jobradar-user.session    your Telegram login — only if you ask for it
    resume.pdf / resume.txt  your résumé, if uploaded

It contains your keys (and, optionally, full access to your Telegram account),
so keep it private.

Restoring checks the file first (only these names, sane sizes, real SQLite
files, a readable config), moves your current files into a
``before-restore-<time>`` folder, then puts the backup in place. File paths in
the restored config are normalised to the standard names, so a backup from
another computer works here.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import load

MANIFEST = "jobradar-backup.json"
NAMES = {"config": "config.toml", "database": "jobradar.db", "session": "jobradar-user.session",
         "resume_pdf": "resume.pdf", "resume_text": "resume.txt"}
CONFIG_KEYS = {"database": "state_path", "session": "telegram_session_path",
               "resume_pdf": "resume_pdf_path", "resume_text": "resume_text_path"}
MAX_TOTAL = 300 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"


class BackupError(ValueError):
    """The backup file can't be used (message is shown to the user)."""


def _resolve(base: Path, p: str | None) -> Path | None:
    if not p:
        return None
    path = Path(p).expanduser()
    return path if path.is_absolute() else base / path


def data_files(config_path: Path) -> dict[str, Path]:
    """The files that make up this installation, by role (existing or not)."""
    cfg = load(config_path)
    base = config_path.parent
    db = _resolve(base, cfg.state_path)
    if db is not None and db.suffix.lower() == ".json":
        db = db.with_suffix(".db")
    session = _resolve(base, cfg.telegram_session_path)
    if session is not None and not str(session).endswith(".session"):
        session = Path(str(session) + ".session")
    files = {"config": config_path, "database": db, "session": session,
             "resume_pdf": _resolve(base, cfg.resume_pdf_path or "resume.pdf"),
             "resume_text": _resolve(base, cfg.resume_text_path)}
    return {k: v for k, v in files.items() if v is not None}


def _sqlite_snapshot(path: Path, live: sqlite3.Connection | None = None) -> bytes:
    """A consistent copy of a SQLite file (SQLite's online backup), even while
    another connection is writing to it."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        src = live or sqlite3.connect(str(path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            if live is None:
                src.close()
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)


def make_backup(config_path: str | Path, *, include_login: bool = False, state=None) -> bytes:
    """The backup .zip as bytes. ``state`` (the open :class:`State`) lets the
    database snapshot use the live connection."""
    config_path = Path(config_path)
    files = data_files(config_path)
    buf = io.BytesIO()
    included: dict[str, str] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if files["config"].exists():
            z.writestr(NAMES["config"], files["config"].read_bytes())
            included["config"] = NAMES["config"]
        db = files.get("database")
        if db is not None and db.exists():
            if state is not None and Path(state.path).resolve() == db.resolve():
                with state._lock:
                    data = _sqlite_snapshot(db, state._db)
            else:
                data = _sqlite_snapshot(db)
            z.writestr(NAMES["database"], data)
            included["database"] = NAMES["database"]
        session = files.get("session")
        if include_login and session is not None and session.exists():
            z.writestr(NAMES["session"], _sqlite_snapshot(session))
            included["session"] = NAMES["session"]
        for role in ("resume_pdf", "resume_text"):
            p = files.get(role)
            if p is not None and p.exists():
                z.writestr(NAMES[role], p.read_bytes())
                included[role] = NAMES[role]
        z.writestr(MANIFEST, json.dumps({
            "app": "jobradar", "version": __version__,
            "created": datetime.now(timezone.utc).isoformat(), "files": included,
            "includes_telegram_login": "session" in included,
        }, indent=2))
    return buf.getvalue()


def read_backup(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate a backup and return (manifest, {role: bytes})."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BackupError("That isn't a Job Radar backup (not a .zip file).") from e
    allowed = set(NAMES.values()) | {MANIFEST}
    infos = z.infolist()
    names = [i.filename for i in infos]
    if MANIFEST not in names or NAMES["config"] not in names:
        raise BackupError("That isn't a Job Radar backup (missing jobradar-backup.json or config.toml).")
    unexpected = [n for n in names if n not in allowed]
    if unexpected:
        raise BackupError(f"Unexpected file in the backup: {unexpected[0]!r}. Nothing was restored.")
    if len(names) != len(set(names)) or sum(i.file_size for i in infos) > MAX_TOTAL:
        raise BackupError("The backup is malformed or too large. Nothing was restored.")
    try:
        manifest = json.loads(z.read(MANIFEST))
    except ValueError as e:
        raise BackupError("The backup's description file is unreadable.") from e
    if manifest.get("app") != "jobradar":
        raise BackupError("That isn't a Job Radar backup.")
    by_name = {v: k for k, v in NAMES.items()}
    contents = {by_name[n]: z.read(n) for n in names if n != MANIFEST}
    try:
        tomllib.loads(contents["config"].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise BackupError("The config.toml in the backup is unreadable. Nothing was restored.") from e
    for role in ("database", "session"):
        if role in contents and not contents[role].startswith(_SQLITE_MAGIC):
            raise BackupError(f"The {NAMES[role]} in the backup is damaged. Nothing was restored.")
    if "resume_pdf" in contents and not contents["resume_pdf"].startswith(b"%PDF-"):
        raise BackupError("The resume.pdf in the backup isn't a PDF. Nothing was restored.")
    return manifest, contents


def _write(path: Path, data: bytes, private: bool = False) -> None:
    tmp = path.with_name(path.name + ".restoring")
    tmp.write_bytes(data)
    if private and os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def restore_backup(data: bytes, config_path: str | Path) -> dict[str, Any]:
    """Replace this installation's files with a backup's. The database must
    not be open (the caller closes it). Returns what was restored and where the
    previous files were put."""
    from .web import configfile as cf

    config_path = Path(config_path)
    manifest, contents = read_backup(data)
    base = config_path.parent
    base.mkdir(parents=True, exist_ok=True)

    # 1. Move the current files aside (never delete them).
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = base / f"before-restore-{stamp}"
    current = data_files(config_path) if config_path.exists() else {}
    moved = []
    for role, path in current.items():
        extras = [Path(str(path) + s) for s in ("-wal", "-shm", "-journal")]
        for p in [path] + extras:
            if p.is_file():
                safety.mkdir(exist_ok=True)
                os.replace(p, safety / p.name)
                moved.append(p.name)

    # 2. Put the backup in place under the standard names, and point the
    #    restored config at them (a backup may come from another computer).
    raw = tomllib.loads(contents["config"].decode("utf-8"))
    for role, key in CONFIG_KEYS.items():
        if role in contents or key in raw:
            raw[key] = NAMES[role]       # standard name beside the config
        if role == "resume_text" and role not in contents:
            raw.pop(key, None)           # no résumé text in the backup
    cf.write(config_path, raw)           # (the old config was moved aside, so no .bak is made)
    for role in ("database", "session", "resume_pdf", "resume_text"):
        if role in contents:
            _write(base / NAMES[role], contents[role], private=(role == "session"))

    # 3. A snapshot may carry a run lock from the moment it was taken.
    if "database" in contents:
        db = sqlite3.connect(str(base / NAMES["database"]))
        try:
            db.execute("DELETE FROM meta WHERE key = 'run_lock'")
            db.commit()
        except sqlite3.Error:
            pass
        finally:
            db.close()
    return {"restored": sorted(NAMES[r] for r in contents), "backup_created": manifest.get("created"),
            "previous_files": str(safety) if moved else None}
