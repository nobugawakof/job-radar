"""This computer's network addresses, to show how to open the dashboard from
another device (a phone on the same Wi-Fi, or anywhere through Tailscale)."""

from __future__ import annotations

import ipaddress
import socket

_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")   # Tailscale gives each device an address in here


def _primary_ipv4() -> str | None:
    """The address this computer uses to reach other networks. (A UDP
    "connect" only picks a route; nothing is sent.)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def addresses() -> list[dict[str, str]]:
    """[{"ip": "192.168.1.20", "kind": "network"}, {"ip": "100.x.y.z", "kind": "tailscale"}],
    home-network addresses first; no loopback or link-local addresses."""
    found: list[str] = []
    primary = _primary_ipv4()
    if primary:
        found.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out, seen = [], set()
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if ip in seen or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        seen.add(ip)
        out.append({"ip": ip, "kind": "tailscale" if addr in _TAILSCALE else "network"})
    return sorted(out, key=lambda a: a["kind"] == "tailscale")
