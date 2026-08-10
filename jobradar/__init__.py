"""Social Job Radar — Telegram edition.

A self-hosted bot that scrapes social platforms for job postings, keeps the
ones matching the configured keywords and remote preference, and delivers them
via Telegram. The only persistence is a small JSON state file that remembers
which postings were already sent.

The package is organised so that adding a source means implementing a single
collector interface and never touching filtering, dedup, or delivery.
"""

__version__ = "0.1.0"
