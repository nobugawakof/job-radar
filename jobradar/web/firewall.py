"""Open the dashboard's port in Windows Firewall, so other computers can reach it.

Windows asks about a new program on the server's own screen; on a server
nobody watches, the answer never comes and the port stays closed. When Job
Radar runs with administrator rights it adds (or updates) one inbound rule
for its port itself. Without those rights it says how to do it.

A cloud server (AWS, Azure, ...) has a second firewall outside Windows (a
security group); that one is opened in the cloud's own console.
"""

from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("jobradar.web")
RULE = "Job Radar dashboard"


def _netsh(*args: str) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["netsh", "advfirewall", "firewall", *args], capture_output=True, text=True,
                          timeout=20, creationflags=flags)


def command(port: int) -> str:
    """What to run by hand (PowerShell as administrator) if Job Radar can't."""
    return (f'New-NetFirewallRule -DisplayName "{RULE}" -Direction Inbound -Protocol TCP '
            f'-LocalPort {port} -Action Allow -Profile Any')


def ensure_open(port: int) -> dict[str, object]:
    """Make sure inbound TCP ``port`` is allowed. Returns {"status": ..., "message": ...}
    with status "open" (rule already there / added), "manual" (no rights:
    run the command), "skipped" (not Windows) or "error"."""
    if not sys.platform.startswith("win"):
        return {"status": "skipped", "message": "Not Windows: allow the port in this computer's firewall if it has one."}
    try:
        shown = _netsh("show", "rule", f"name={RULE}")
        if shown.returncode == 0 and f"{port}" in shown.stdout and "Allow" in shown.stdout:
            return {"status": "open", "message": f"Windows Firewall allows port {port}."}
        if shown.returncode == 0:      # a rule from an older port: replace it
            _netsh("delete", "rule", f"name={RULE}")
        added = _netsh("add", "rule", f"name={RULE}", "dir=in", "action=allow", "protocol=TCP",
                       f"localport={port}", "profile=any")
    except (OSError, subprocess.SubprocessError) as e:
        return {"status": "error", "message": f"Couldn't check Windows Firewall: {e}"}
    if added.returncode == 0:
        log.info("Windows Firewall: opened port %s for the dashboard", port)
        return {"status": "open", "message": f"Opened port {port} in Windows Firewall."}
    return {"status": "manual",
            "message": "Job Radar isn't running as administrator, so it couldn't open the port in Windows "
                       "Firewall. Run Job Radar as administrator once, or run this in PowerShell as administrator:",
            "command": command(port)}
