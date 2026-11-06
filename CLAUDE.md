@AGENTS.md

## Claude Code notes

- `.claude/settings.json` allows only the five ERP read tools and denies the three write tools and Call Assistant Model; `.claude/hooks/pre-tool-use.py` denies anything not in `policy/tool-policy.yaml` and appends a JSONL audit line per call. A hook deny applies even in bypass permissions mode.
- `.claude/agents/extractor.md` has Read only and no MCP; `.claude/agents/checker.md` has read only MCP. Do not widen either.
- Skills live in `skills/`; `.claude/skills/` is a mirror written by `scripts/sync-skills.py`. Edit the source, then sync, or `make check` fails on drift.
