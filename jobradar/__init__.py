"""Social Job Radar — Phase 1.

A self-hosted collector that monitors social platforms for job postings,
filters them against each user's keywords / remote preference / work
eligibility, and delivers the survivors via Telegram and a web dashboard.

The package is organised so that adding a source means implementing a single
collector interface (NFR-13) and never touching filtering, storage, or
delivery.
"""

__version__ = "0.1.0"
