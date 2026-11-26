---
name: add-config-option
description: Add or change a Job Radar setting end to end, from the Config dataclass through dashboard validation, the dashboard form and the docs. Use whenever a new config key is introduced or an existing one changes its type, default or choices.
---

# Add a setting

A setting is defined in up to six places. Missing one shows up as:
- the dashboard silently dropping the value;
- the README disagreeing with the code (as with B7).

1. **`jobradar/config.py`**: add the field to `Config`, with a type, a default
   and a short trailing comment. Handle old names, if any, in the loader (see
   how `send_unknown_location` maps to `unknown_location`).
2. **`jobradar/web/configfile.py`**:
   - add the key to the right `SECTIONS` group (order = order in the file);
   - fixed choices → `_CHOICES["key"] = (...)`;
   - secrets → `SECRET_KEYS`, plus `ENV_FOR` if an env var can set it;
   - multi-line text whose whitespace matters (templates) → add it to the
     no-strip check in the `str` branch (look for `apply_template_zh`).
3. **`jobradar/web/index.html`**: add an input with `data-k="key"`.
   `bindInputs()` and `fillInputs()` handle it automatically:
   - checkbox → bool;
   - `type=number` → number;
   - `.chips` → list;
   - `data-secret` → never shown back.
4. **`config.example.toml`**: the key with its default and a one-line comment.
5. **`README.md`**: the section where the feature is explained.
6. **Tests**: a test of the behaviour. If the setting changes a default path,
   pin it explicitly in older tests that assume the old behaviour. For example,
   English-path tests set `message_language="en"`.

Verify:
- `python3 -m unittest discover -s tests`;
- save the setting once from the dashboard (skill `verify-dashboard`) and check
  that it lands in `config.toml`.
