"""Web dashboard.

A standard-library ``http.server`` app — no web framework, no external
dependency, binds to localhost by default (IR-8). It authenticates each user
and shows only their own postings and settings (IR-5, NFR-8), provides the
browse/filter/sort and status-marking of FR-34/FR-35, and exposes the owner's
administrative views (FR-37-39) which are hidden from members.
"""

from .app import AppContext, build_server, make_handler  # noqa: F401
