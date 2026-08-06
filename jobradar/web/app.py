"""Dashboard HTTP handler.

Deliberately small and framework-free. Routing is a table of
``(method, path) -> handler`` plus a couple of prefix routes. Rendering is
server-side HTML built with :func:`html.escape` everywhere untrusted text is
interpolated. Sessions are a signed-free opaque per-user token stored in a
cookie; because the dashboard binds to localhost (IR-8) this is sufficient for
the closed, self-hosted deployment the SRS describes.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .. import geo
from ..config import Config
from ..models import (
    STATUS_APPLIED,
    STATUS_DISMISSED,
    STATUS_NEW,
    STATUS_SAVED,
)
from ..repos import Store


@dataclass
class AppContext:
    store: Store
    config: Config
    # FR-39: trigger a manual run on demand. Injected so the web layer stays
    # decoupled from the pipeline.
    trigger_run: Callable[[], Any] | None = None


def _page(title: str, body: str, *, nav: str = "") -> bytes:
    return (
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
line-height:1.45;background:#f6f7f9;color:#111}}
@media (prefers-color-scheme:dark){{body{{background:#14161a;color:#e6e6e6}}
.card{{background:#1d2026!important;border-color:#2a2e37!important}}
a{{color:#7db2ff}} input,select,button{{background:#1d2026;color:#e6e6e6;border-color:#2a2e37}}}}
header{{background:#0d1b2a;color:#fff;padding:.8rem 1rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap}}
header a{{color:#cfe3ff;text-decoration:none;font-size:.95rem}}
main{{max-width:900px;margin:1rem auto;padding:0 1rem}}
.card{{background:#fff;border:1px solid #e2e5ea;border-radius:10px;padding:.9rem 1rem;margin:.6rem 0}}
.title{{font-weight:600;font-size:1.05rem;margin:0 0 .2rem}}
.meta{{color:#667085;font-size:.85rem}}
.tier{{display:inline-block;font-size:.7rem;padding:.05rem .35rem;border-radius:4px;background:#eef;color:#334;margin-left:.3rem}}
form.inline{{display:inline}}
button,input,select{{font:inherit;padding:.35rem .5rem;border:1px solid #ccc;border-radius:6px}}
button{{cursor:pointer;background:#0d6efd;color:#fff;border:none}}
button.secondary{{background:#6c757d}}
.filters{{display:flex;gap:.5rem;flex-wrap:wrap;margin:.5rem 0}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:.3rem .5rem;font-size:.85rem;text-align:left}}
.pill{{font-size:.72rem;background:#e7f0ff;color:#0d3b8a;border-radius:10px;padding:.1rem .5rem;margin-right:.2rem}}
</style></head><body>
<header><strong>🛰️ Job Radar</strong>{nav}</header>
<main>{body}</main></body></html>"""
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    ctx: AppContext  # set by make_handler

    server_version = "JobRadar/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        return

    # ---- helpers ----------------------------------------------------------
    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        out: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _current_user(self) -> dict[str, Any] | None:
        token = self._cookies().get("sid", "")
        return self.ctx.store.get_user_by_dashboard_token(token)

    def _send(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(data).items()}

    def _nav(self, user: dict[str, Any]) -> str:
        links = ['<a href="/">Postings</a>', '<a href="/review">Review</a>',
                 '<a href="/settings">Settings</a>']
        if user["is_owner"]:
            links.append('<a href="/admin">Admin</a>')
        links.append('<a href="/export">Export</a>')
        links.append('<a href="/logout">Logout</a>')
        return " · ".join(links)

    # ---- dispatch ---------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path == "/login":
            return self._login_page()
        if path == "/logout":
            return self._redirect("/login", cookie="sid=; Max-Age=0; Path=/")

        user = self._current_user()
        if not user:
            return self._redirect("/login")

        if path == "/":
            return self._dashboard(user, qs)
        if path == "/review":
            return self._review_page(user)
        if path == "/settings":
            return self._settings_page(user)
        if path == "/export":
            return self._export(user)
        if path == "/admin":
            return self._admin_page(user)
        return self._send(_page("Not found", "<p>Not found.</p>"), status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/login":
            return self._do_login()

        user = self._current_user()
        if not user:
            return self._redirect("/login")
        form = self._read_form()

        if path == "/status":
            return self._mark_status(user, form)
        if path == "/review":
            return self._resolve_review(user, form)
        if path == "/settings":
            return self._save_settings(user, form)
        if path == "/admin/member":
            return self._admin_add_member(user, form)
        if path == "/admin/member/delete":
            return self._admin_delete_member(user, form)
        if path == "/admin/run":
            return self._admin_run(user)
        return self._send(_page("Not found", "<p>Not found.</p>"), status=404)

    # ---- auth -------------------------------------------------------------
    def _login_page(self, error: str = "") -> None:
        err = f'<p style="color:#c00">{html.escape(error)}</p>' if error else ""
        body = f"""<div class="card"><h2>Sign in</h2>{err}
<form method="post" action="/login">
<p>Enter your dashboard token (issued by the owner):</p>
<input name="token" style="width:70%" autofocus>
<button type="submit">Sign in</button></form></div>"""
        self._send(_page("Sign in", body))

    def _do_login(self) -> None:
        form = self._read_form()
        token = form.get("token", "").strip()
        user = self.ctx.store.get_user_by_dashboard_token(token)
        if not user:
            return self._login_page("Invalid token.")
        # HttpOnly session cookie scoped to localhost.
        self._redirect("/", cookie=f"sid={token}; HttpOnly; Path=/; SameSite=Lax")

    # ---- dashboard (FR-34 / FR-35) ---------------------------------------
    def _dashboard(self, user: dict[str, Any], qs: dict[str, list[str]]) -> None:
        source = _first(qs, "source")
        keyword = _first(qs, "keyword")
        sort = _first(qs, "sort") or "posted_at"
        has_salary = None
        if _first(qs, "salary") == "1":
            has_salary = True
        status_filter = _first(qs, "status")
        statuses = [status_filter] if status_filter else None

        postings = self.ctx.store.list_user_postings(
            user["id"], source=source or None, keyword=keyword or None,
            has_salary=has_salary, statuses=statuses, sort=sort,
        )

        # Build filter controls.
        all_sources = sorted({p["source"] for p in
                              self.ctx.store.list_user_postings(user["id"])})
        src_opts = "".join(
            f'<option value="{html.escape(s)}"{" selected" if s==source else ""}>{html.escape(s)}</option>'
            for s in all_sources
        )
        sort_opts = "".join(
            f'<option value="{v}"{" selected" if v==sort else ""}>{label}</option>'
            for v, label in [("posted_at", "Date"), ("source", "Source"),
                             ("salary", "Salary"), ("title", "Title")]
        )
        filters = f"""<form class="filters" method="get" action="/">
<select name="source"><option value="">All sources</option>{src_opts}</select>
<input name="keyword" placeholder="keyword" value="{html.escape(keyword or '')}">
<label><input type="checkbox" name="salary" value="1" {"checked" if has_salary else ""}> has salary</label>
<select name="sort">{sort_opts}</select>
<button type="submit">Apply</button></form>"""

        if not postings:
            cards = '<div class="card"><em>No postings match. Silence means nothing matched.</em></div>'
        else:
            cards = "".join(self._posting_card(p) for p in postings)

        body = f"<h2>Your postings ({len(postings)})</h2>{filters}{cards}"
        self._send(_page("Postings", body, nav=self._nav(user)))

    def _posting_card(self, p: dict[str, Any]) -> str:
        title = html.escape(p.get("title") or "(untitled)")
        url = html.escape(p.get("source_url") or "#")
        origins = ", ".join(p.get("origins") or [p.get("source")])
        tier = html.escape(p.get("source_tier", ""))
        kws = "".join(f'<span class="pill">{html.escape(k)}</span>' for k in p.get("matched_keywords", []))
        sal = p.get("salary_raw") or (f"{p['salary_currency'] or ''} {int(p['salary_min']):,}" if p.get("salary_min") else "")
        sal_html = f'<span class="meta">💰 {html.escape(str(sal))}</span> · ' if sal else ""
        remote = p.get("is_remote")
        status = p.get("status", "new")
        pid = html.escape(p["id"])
        actions = "".join(
            f'<form class="inline" method="post" action="/status">'
            f'<input type="hidden" name="posting_id" value="{pid}">'
            f'<input type="hidden" name="action" value="{a}">'
            f'<button class="secondary" type="submit">{label}</button></form> '
            for a, label in [("applied", "Applied"), ("saved", "Save"), ("dismissed", "Dismiss")]
        )
        return f"""<div class="card">
<p class="title"><a href="{url}" target="_blank" rel="noopener">{title}</a>
<span class="tier">{tier}</span></p>
<p class="meta">{sal_html}📡 {html.escape(origins)} · {html.escape(str(remote))} · status: {html.escape(status)}</p>
<p>{kws}</p>
{actions}</div>"""

    def _mark_status(self, user: dict[str, Any], form: dict[str, str]) -> None:
        pid = form.get("posting_id", "")
        action = form.get("action", "")
        mapping = {"applied": STATUS_APPLIED, "saved": STATUS_SAVED, "dismissed": STATUS_DISMISSED}
        if action in mapping and self.ctx.store.user_has_posting(user["id"], pid):
            # FR-35: dismissed never reappears (list query excludes it).
            self.ctx.store.set_user_posting_status(user["id"], pid, mapping[action])
        self._redirect(self.headers.get("Referer") or "/")

    # ---- review (FR-21 / FR-23) ------------------------------------------
    def _review_page(self, user: dict[str, Any]) -> None:
        items = self.ctx.store.review_queue(user["id"])
        if not items:
            body = "<h2>Review queue</h2><div class='card'><em>Empty ✅</em></div>"
            return self._send(_page("Review", body, nav=self._nav(user)))
        cards = []
        for p in items:
            excerpt = html.escape(" ".join((p.get("description") or "").split())[:280])
            pid = html.escape(p["id"])
            cards.append(f"""<div class="card">
<p class="title">{html.escape(p.get('title') or '(untitled)')}</p>
<p class="meta">📡 {html.escape(', '.join(p.get('origins') or [p.get('source')]))} — location unclear</p>
<p><i>{excerpt}</i></p>
<form class="inline" method="post" action="/review">
<input type="hidden" name="posting_id" value="{pid}">
<button name="decision" value="relevant">✅ Relevant</button></form>
<form class="inline" method="post" action="/review">
<input type="hidden" name="posting_id" value="{pid}">
<button class="secondary" name="decision" value="not_relevant">❌ Not relevant</button></form>
</div>""")
        body = f"<h2>Review queue ({len(items)})</h2>{''.join(cards)}"
        self._send(_page("Review", body, nav=self._nav(user)))

    def _resolve_review(self, user: dict[str, Any], form: dict[str, str]) -> None:
        pid = form.get("posting_id", "")
        decision = form.get("decision", "")
        if not self.ctx.store.user_has_posting(user["id"], pid):
            return self._redirect("/review")
        posting = self.ctx.store.get_posting(pid)
        source = posting["source"] if posting else None
        if decision == "relevant":
            self.ctx.store.set_user_posting_status(user["id"], pid, STATUS_NEW, resolved=True)
            self.ctx.store.record_resolution(user["id"], pid, "relevant", source)
        else:
            self.ctx.store.set_user_posting_status(user["id"], pid, STATUS_DISMISSED, resolved=True)
            self.ctx.store.record_resolution(user["id"], pid, "not_relevant", source)
        self._redirect("/review")

    # ---- settings (FR-36) -------------------------------------------------
    def _settings_page(self, user: dict[str, Any]) -> None:
        suggestions = self.ctx.store.suggested_rules(user["id"])
        sug_html = ""
        if suggestions:
            sug_html = "<div class='card'><b>💡 Suggested rules</b><ul>" + "".join(
                f"<li>{html.escape(s['suggestion'])}</li>" for s in suggestions
            ) + "</ul></div>"
        body = f"""<h2>Settings</h2>{sug_html}
<form method="post" action="/settings" class="card">
<p><label>Keywords (comma-separated)<br>
<input name="keywords" style="width:90%" value="{html.escape(', '.join(user['keywords']))}"></label></p>
<p><label>Eligible countries (names or ISO codes, comma-separated)<br>
<input name="countries" style="width:90%" value="{html.escape(', '.join(user['eligible_countries']))}"></label></p>
<p><label><input type="checkbox" name="remote_only" value="1" {"checked" if user['remote_only'] else ""}> Remote roles only</label></p>
<p><label>Mute for hours (0 = unmuted): <input name="mute_hours" value="0" style="width:5rem"></label></p>
<button type="submit">Save</button></form>"""
        self._send(_page("Settings", body, nav=self._nav(user)))

    def _save_settings(self, user: dict[str, Any], form: dict[str, str]) -> None:
        from datetime import timedelta
        from ..db import utcnow

        keywords = [k.strip().lower() for k in form.get("keywords", "").split(",") if k.strip()]
        countries = geo.normalise_country_list(
            [c.strip() for c in form.get("countries", "").split(",") if c.strip()]
        )
        remote_only = form.get("remote_only") == "1"
        fields: dict[str, Any] = {
            "keywords": keywords,
            "eligible_countries": countries,
            "remote_only": remote_only,
        }
        mute_hours = form.get("mute_hours", "0")
        if mute_hours.isdigit() and int(mute_hours) > 0:
            fields["muted_until"] = utcnow() + timedelta(hours=int(mute_hours))
        else:
            fields["muted_until"] = None
        self.ctx.store.update_user(user["id"], **fields)
        self._redirect("/settings")

    # ---- export / deletion (DR-5) ----------------------------------------
    def _export(self, user: dict[str, Any]) -> None:
        data = self.ctx.store.export_user(user["id"])
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=jobradar-export.json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- admin (FR-37 / FR-38 / FR-39, IR-7) -----------------------------
    def _admin_page(self, user: dict[str, Any]) -> None:
        if not user["is_owner"]:
            return self._send(_page("Forbidden", "<p>Members only see their own data.</p>"), status=403)

        members = self.ctx.store.list_users()
        member_rows = "".join(self._member_row(m) for m in members)

        sources = self.ctx.store.list_sources()
        src_rows = "".join(
            f"<tr><td>{html.escape(s['name'])}</td><td>{html.escape(s['type'])}</td>"
            f"<td>{s['tier']}</td><td>{'on' if s['enabled'] else 'off'}"
            f"{' (blocked)' if s['blocked'] else ''}</td>"
            f"<td>{s['consecutive_failures']}</td>"
            f"<td>{html.escape(s['last_success_at'] or '—')}</td></tr>"
            for s in sources
        )

        runs = self.ctx.store.run_history(limit=10)
        run_rows = []
        for r in runs:
            per_src = "; ".join(
                f"{s['source']}: {s['items_fetched']}f/{s['postings_detected']}p"
                + (f" ERR({html.escape(s['error'] or '')})" if s["status"] == "error" else "")
                for s in r["sources"]
            )
            run_rows.append(
                f"<tr><td>{r['id']}</td><td>{html.escape(r['trigger'])}</td>"
                f"<td>{html.escape(r['started_at'])}</td><td>{html.escape(per_src)}</td></tr>"
            )

        body = f"""<h2>Admin</h2>
<div class="card"><b>Members</b> (FR-37)
<table><tr><th>Name</th><th>Role</th><th>Telegram</th><th>Dashboard token</th><th></th></tr>{member_rows}</table>
<form method="post" action="/admin/member" style="margin-top:.6rem">
<input name="name" placeholder="new member name" required>
<button type="submit">Add member</button></form></div>

<div class="card"><b>Run history</b> (FR-38)
<form class="inline" method="post" action="/admin/run"><button>▶ Trigger run now (FR-39)</button></form>
<table><tr><th>#</th><th>Trigger</th><th>Started</th><th>Per source</th></tr>{''.join(run_rows) or '<tr><td colspan=4>No runs yet</td></tr>'}</table></div>

<div class="card"><b>Sources</b> (SR-2)
<table><tr><th>Name</th><th>Type</th><th>Tier</th><th>State</th><th>Fails</th><th>Last success</th></tr>{src_rows or '<tr><td colspan=6>None</td></tr>'}</table></div>"""
        self._send(_page("Admin", body, nav=self._nav(user)))

    def _member_row(self, m: dict[str, Any]) -> str:
        role = "owner" if m["is_owner"] else "member"
        tg = "linked" if m["telegram_chat_id"] else "not linked"
        token = html.escape(m.get("dashboard_token") or "—")
        if m["is_owner"]:
            remove = ""
        else:
            remove = (
                '<form class="inline" method="post" action="/admin/member/delete">'
                f'<input type="hidden" name="user_id" value="{html.escape(m["id"])}">'
                '<button class="secondary">Remove</button></form>'
            )
        return (
            f"<tr><td>{html.escape(m['name'])}</td><td>{role}</td><td>{tg}</td>"
            f"<td><code>{token}</code></td><td>{remove}</td></tr>"
        )

    def _admin_add_member(self, user: dict[str, Any], form: dict[str, str]) -> None:
        if not user["is_owner"]:
            return self._send(_page("Forbidden", "<p>Forbidden.</p>"), status=403)
        name = form.get("name", "").strip()
        if name:
            uid = self.ctx.store.create_user(
                name,
                keywords=self.ctx.config.default_keywords,
                eligible_countries=self.ctx.config.default_eligible_countries,
                remote_only=self.ctx.config.default_remote_only,
            )
            self.ctx.store.issue_dashboard_token(uid)
            self.ctx.store.issue_link_code(uid)
        self._redirect("/admin")

    def _admin_delete_member(self, user: dict[str, Any], form: dict[str, str]) -> None:
        if not user["is_owner"]:
            return self._send(_page("Forbidden", "<p>Forbidden.</p>"), status=403)
        uid = form.get("user_id", "")
        target = self.ctx.store.get_user(uid)
        if target and not target["is_owner"]:
            self.ctx.store.delete_user(uid, hard=True)  # DR-5 full deletion
        self._redirect("/admin")

    def _admin_run(self, user: dict[str, Any]) -> None:
        if not user["is_owner"]:
            return self._send(_page("Forbidden", "<p>Forbidden.</p>"), status=403)
        if self.ctx.trigger_run:
            self.ctx.trigger_run()
        self._redirect("/admin")


def _first(qs: dict[str, list[str]], key: str) -> str | None:
    v = qs.get(key)
    return v[0] if v else None


def make_handler(ctx: AppContext) -> type[Handler]:
    return type("BoundHandler", (Handler,), {"ctx": ctx})


def build_server(ctx: AppContext) -> ThreadingHTTPServer:
    handler = make_handler(ctx)
    # IR-8: bind to localhost by default; wider exposure is an explicit choice.
    server = ThreadingHTTPServer((ctx.config.web_host, ctx.config.web_port), handler)
    return server
