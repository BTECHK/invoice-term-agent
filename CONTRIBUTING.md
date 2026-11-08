# Contributing

These are the rules for changing this repository. They apply to people and artificial intelligence (AI) coding agents alike.

Identifiers such as FR-011 (functional requirement), BR-4 (business rule), ADR-003 (architecture decision record) and T-22 (threat) are explained in [docs/README.md](docs/README.md).

## Set up

```
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
make demo
```

`make demo` runs offline: it uses the mock ERP data service and the reader transcripts in `transcripts/`, so it needs no key. `make read` writes those transcripts with the local OCR reader, which also runs offline. This build refuses live runs. `.env.example` documents future integration placeholders; never commit `.env`.

When you edit the repository with a coding agent, set `AP_AGENT_DEV=1` before launching it. The hook then lets the agent use its built-in tools and edit protected paths, and records the role `person`. The three ERP write tools, the nested model tool and the hook log stay denied in this mode. Set it yourself; an agent must not, and CI and demo runs do not use it.

## Before you change anything

1. Read the rules for AI coding agents in [docs/README.md](docs/README.md).
2. Read the binding non-goals in [docs/01-prd.md](docs/01-prd.md). A change to one needs a recorded decision and a new ADR.
3. Find the requirement your change serves in [docs/02-srs.md](docs/02-srs.md). If none fits, add one first.

## Make targets

| Command | What it does |
|---|---|
| `make read` | Reads the five golden invoices and the injection case with the local OCR reader and rewrites `transcripts/` |
| `make demo` | Runs the five golden invoices and injection case end to end and writes results, proposals, worklist records and audit traces under `OUTPUT` |
| `make test` | Runs the essential workflow and control behavior scenarios |
| `make eval` | Grades the golden set and the injection case against `evals/thresholds.json` |
| `make check` | Runs `scripts/check.py`: skill frontmatter, schemas, mirror drift, allowlist against policy, hook wiring, the deny list, subagent tool lists and turn caps against policy, secret patterns, dashes |

## Change recipes

| Change | Do this | Gate |
|---|---|---|
| Add an exception skill | Create `skills/<name>/SKILL.md` with spec-only frontmatter. List its fixed queries in `integration/agent-tools.yaml`. Run `python3 scripts/sync-skills.py`. Add an FR with a fit criterion, a distinct behavior scenario or review record, and a trace row | test, eval, check |
| Change a rule or a threshold | Edit `src/invoice_agent/rules.py` or `policy/term-rules.yaml`. Update the BR and FR text and the relevant behavior scenario or review record | test, eval |
| Add or change an ERP query | Edit `integration/agent-tools.yaml`. Declare each `${...}` variable with a pattern and a length. Update the mock fixtures | test, check |
| Add a tool or widen a role | Change `policy/tool-policy.yaml` first, then `.claude/settings.json`. Add a T-# row. Never allow an ERP write tool | check, review |
| Change a prompt, a skill body or a model pin | Pin exact versions in `policy/models.yaml`. Run the eval and put the scores in the change description. Keep the prior pins for rollback | eval |
| Change a schema | Update the schema, the writer, the fixtures and section 5.4 of [docs/04-architecture.md](docs/04-architecture.md) together | test, check |
| Add a labelled case from a worklist decision | Add the HTML source under `evals/golden/source/` and render it with `python3 scripts/render_invoices.py`. Run `make read` and check the new transcript against the page. Add the expected entry in `evals/golden/expected.json`, and add the input's path to `DEFAULT_INPUTS` in `src/invoice_agent/cli.py` so `make demo` runs it. Use a described, anonymized shape, never a real invoice | eval |

## Tests

- Keep behavior checks in `tests/test_workflow.py` or `tests/test_controls.py`. `tests/test_hardening.py` holds the hardening regressions: grader reason codes, supplier legal form, guard folding, text-source quotes and the PreToolUse hook (crafted events must be denied or fail closed, and development mode must keep the write tools, the nested model and the hook log denied). Reuse an end-to-end scenario when possible; add a method only for a distinct observable failure risk.
- `tests/test_reader.py` holds the reader checks: the golden invoices read with the network blocked, and the committed transcripts compared with fresh reader output.
- Tests run offline. No network, no sleeps, no live key.
- Fixtures come from the golden invoices and their POs. Do not add real supplier data.

## Code and docs style

- Comments explain why, never what. No TODO banners, no "added by" notes, no divider comments, no commented-out code.
- Docs follow the style list at the end of [docs/README.md](docs/README.md): no em or en dashes, no emoji, sentence case headings, short paragraphs.
- Model IDs and provider names appear only in `policy/models.yaml`.

## Definition of done

- `make test`, `make eval` and `make check` pass, and you report their output.
- The trace table in [docs/README.md](docs/README.md) matches your change.
- A T-# row exists for any new tool, role, data source or write path.
- No secret, token or full bank number is in any file.
- The change touches only what its requirement needs.

## Changes and descriptions

One change has one purpose. Name the FR, ADR and T IDs it touches. List the commands you ran and what they printed.
