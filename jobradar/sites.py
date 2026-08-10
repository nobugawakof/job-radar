"""Turn a plain site URL into a source definition.

Lets the config list sites as simple URL arrays (``en = [...]``, ``cn = [...]``)
instead of verbose ``[[sources]]`` blocks. Known sites map to the right
collector automatically; unknown ones fall back to RSS (if the URL looks like a
feed) or best-effort scraping. Sites with no usable free access are skipped with
a clear warning.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("jobradar.sites")

# Sites with no viable free access (JavaScript-rendered + login/anti-bot walls).
UNSUPPORTED = {
    "zhipin.com": "BOSS直聘", "lagou.com": "拉勾", "liepin.com": "猎聘",
    "zhaopin.com": "智联招聘", "bossjob.us": "Bossjob", "weibo.com": "Weibo",
    "douban.com": "豆瓣", "linkedin.com": "LinkedIn",
}


def _parse(url: str) -> tuple[str, str, str]:
    """Return (host, base-domain, path) for a URL or bare domain."""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    host = (p.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    base = ".".join(parts[-2:]) if len(parts) >= 2 else host
    return host, base, p.path or ""


def _slug(path: str, prefix: str) -> str | None:
    m = re.search(re.escape(prefix) + r"/([^/?#]+)", path)
    return m.group(1) if m else None


def _safe_name(host: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:24] or "site"


def _resolve_one(host: str, base: str, path: str, raw: str) -> list[dict[str, Any]]:
    if host == "news.ycombinator.com" or base == "ycombinator.com":
        return [{"name": "hackernews", "type": "hn", "tier": "A", "classifier_prior": 1.0}]

    if base == "v2ex.com":
        node = _slug(path, "/go")
        if node:
            return [{"name": f"v2ex-{node}", "type": "rss", "tier": "A",
                     "url": f"https://www.v2ex.com/feed/{node}.xml"}]
        return [
            {"name": "v2ex-jobs", "type": "rss", "tier": "A",
             "url": "https://www.v2ex.com/feed/jobs.xml"},
            {"name": "v2ex-remote", "type": "rss", "tier": "A",
             "url": "https://www.v2ex.com/feed/remote.xml"},
        ]

    if base == "ruby-china.org":
        return [{"name": "ruby-china", "type": "rss", "tier": "A",
                 "url": "https://ruby-china.org/topics/feed"}]

    if base == "weworkremotely.com":
        return [{"name": "weworkremotely", "type": "rss", "tier": "A",
                 "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"}]

    if base == "reddit.com":
        # Use the subreddit's public RSS feed — no OAuth app / client_id /
        # secret needed. (Reddit now gates API-app creation behind a developer
        # registration; the RSS feed sidesteps that entirely.) For the full
        # Data API instead, add an explicit [[sources]] block of type "reddit".
        sub = _slug(path, "/r") or "forhire"
        # Reddit rate-limits bursts of unauthenticated RSS hits (429). Space
        # requests to reddit.com well apart so several subreddits can coexist.
        return [{"name": f"reddit-{sub}", "type": "rss", "tier": "A",
                 "request_interval_s": 15.0,
                 "url": f"https://www.reddit.com/r/{sub}/new/.rss"}]

    if base in ("x.com", "twitter.com"):
        return [{"name": "x", "type": "twitter", "tier": "A"}]

    if base in ("bsky.app", "bsky.social"):
        return [{"name": "bluesky", "type": "bluesky", "tier": "A", "query": "hiring remote"}]

    # Generic: a feed-looking URL becomes an RSS source; anything else is a
    # best-effort scrape (Tier B — auto-disables if it breaks/returns nothing).
    low = raw.lower()
    url = raw if "://" in raw else "https://" + raw
    if any(low.endswith(ext) for ext in (".rss", ".xml", ".atom")) or "/feed" in low or "rss" in low:
        return [{"name": _safe_name(host), "type": "rss", "tier": "A", "url": url}]
    return [{"name": _safe_name(host), "type": "scrape", "tier": "B", "url": url}]


def resolve(urls: list[str]) -> list[dict[str, Any]]:
    """Resolve a list of site URLs into source definitions, de-duplicated."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in urls:
        if not raw or not str(raw).strip():
            continue
        host, base, path = _parse(str(raw))
        if base in UNSUPPORTED or host in UNSUPPORTED:
            log.warning("skipping %s — %s is not supported (JavaScript/login-walled; "
                        "no free way to read its job posts)", raw, UNSUPPORTED.get(base, host))
            continue
        for src in _resolve_one(host, base, path, str(raw)):
            if src["name"] in seen:
                continue
            seen.add(src["name"])
            out.append(src)
    return out
