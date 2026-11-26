@AGENTS.md

## Claude Code notes

- The skills in `.claude/skills/` load on demand: `add-config-option`,
  `schema-migration`, `verify-dashboard`, `telegram-account-feature`.
- If the conversation was compacted, re-read `docs/dev/status.md` before you
  pick the next task, and `docs/dev/decisions.md` before you change behaviour.
- Before you finish a piece of work, add anything new you learned (a gotcha, a
  decision, a finished or new open item) to these files in the same commit.
