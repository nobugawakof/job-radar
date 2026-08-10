"""Build a standalone Job Radar executable with PyInstaller.

    python build.py

Produces ``dist/jobradar`` (or ``dist/jobradar.exe`` on Windows). PyInstaller
does not cross-compile, so run this on the OS you want to target — run it on
Windows for a .exe. This is a convenience wrapper around ``pyinstaller
jobradar.spec``; it installs PyInstaller if it isn't already present.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SPEC = Path(__file__).with_name("jobradar.spec")


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found — installing it...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def main() -> int:
    _ensure_pyinstaller()
    print(f"Building from {SPEC.name} ...")
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)])
    ext = ".exe" if sys.platform.startswith("win") else ""
    out = Path("dist") / f"jobradar{ext}"
    print(f"\nDone. Executable: {out}")
    print("Put a config.toml next to it (see config.example.toml) and run it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
