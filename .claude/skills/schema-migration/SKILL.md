---
name: schema-migration
description: Change the SQLite schema of jobradar.db safely (new table, new column, new index) with a user_version bump, backward-compatible migration, State methods and tests. Use for any change to _SCHEMA in jobradar/state.py.
---

# Change the database schema

Users upgrade in place, so an existing `jobradar.db` must keep working, and a
newer database must refuse to open in an older Job Radar.

1. In `jobradar/state.py`:
   - bump `SCHEMA_VERSION` and extend its comment (`# …; 6: <what>`);
   - **new table or index:** add `CREATE TABLE IF NOT EXISTS …` /
     `CREATE INDEX IF NOT EXISTS …` to `_SCHEMA`. `_connect()` runs it on
     every open, so new tables appear on their own;
   - **new column on an existing table:** add it to the `CREATE TABLE` text
     (for new databases) *and* to `_connect()`. Copy the `applications.message`
     example: `PRAGMA table_info(<table>)` → `ALTER TABLE … ADD COLUMN …
     NOT NULL DEFAULT …`. Always give a default.
2. Put all access in `State` methods: one short transaction each, with `?`
   parameters and no SQL built from strings. Return plain dicts.
3. If a column is NOT NULL text, add it to the matching `_*_TEXT_FIELDS`
   coalesce list, so that `None` from older data or the JSON import becomes
   `""`. Forgetting this broke the JSON import once.
4. Check `backup.py` and `datadir.py`. A new table needs no change (the backup
   copies the whole database), but new *files* do.
5. Tests (`tests/test_state.py` or the feature's test file):
   - a fresh database has the new table or column;
   - **upgrade:** build a database with the old shape and `PRAGMA user_version=<old>`,
     open it with `State(path)`, and check that the column exists, old rows are
     intact and the version was bumped;
   - a database with a higher version raises.
6. Update the table list in `docs/dev/architecture.md`.
