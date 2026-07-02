# 04 - Automation

## Purpose

The roadmap control plane should not be a passive board. It should direct implementation by attaching evidence, detecting blockers, creating coding sessions, and generating good agent prompts.

## Evidence Automation

First level: manual command logging.

The operator runs:

```text
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall app -q
cd obsidian-plugin && npm run build
cd frontend && npm run build
```

Then records pass/fail evidence on a card.

Second level: local command runner.

Add a safe endpoint that runs allowlisted verification commands and records output. Do not allow arbitrary shell strings from the browser.

Allowlist examples:

```text
pytest_all        -> .venv/bin/python -m pytest tests -q
compile_backend   -> .venv/bin/python -m compileall app -q
frontend_build    -> cd frontend && npm run build
plugin_build      -> cd obsidian-plugin && npm run build
```

The command runner should be optional and local-only.

## Git Awareness

The portal can read:

- `git status --short`;
- changed files;
- diff stat;
- last commit;
- branch name.

It should not push. It should not commit without explicit operator action.

Card evidence can include:

```text
changed files: app/institute/theses.py, app/api/theses.py, tests/test_theses.py
verification: pytest tests/test_theses.py -q passed
```

## Agent Prompt Generation

Each card can generate a coding-agent prompt:

```text
You are working in institute-one.
Implement card M1-002: Implement thesis domain module and API.

Design links:
- design/local-thesis-alpha/02-thesis-stock-model.md
- roadmap/02-data-model.md

Expected files:
- app/institute/theses.py
- app/api/theses.py
- tests/test_theses.py

Rules:
- use migrations only for schema changes;
- keep tests on echo hand;
- do not introduce hosted infrastructure;
- run verification commands listed below.

Acceptance criteria:
...
```

Prompt generation should be deterministic from the card. The operator can edit before running.

## Coding Session Automation

Starting work should create a session:

```text
card.claim -> session.started -> implementation -> command evidence -> session.completed -> card.review
```

The portal can pre-fill:

- goal from card title and summary;
- planned files from `expected_files`;
- verification commands from card;
- agent prompt from card metadata.

At session close, the operator or agent records:

- touched files;
- what changed;
- tests run;
- remaining risk;
- follow-up cards.

This makes partial implementation visible instead of hidden in chat history.

## Generated Cards

The system can file cards from events:

| Source | Card example |
|---|---|
| Test failure | "Fix failing forecast settlement test." |
| Research parse failure | "Tighten thesis research JSON schema prompt." |
| Data health issue | "Add missing benchmark import for CSI500." |
| Repeated manual workaround | "Add UI action for candidate thesis promotion." |
| Design gap | "Define HK/US currency conversion policy." |

Generated cards start in `Inbox` and require triage.

## Release Gates

A release is a checklist over cards and evidence:

```text
Release A gate:
- all P0/P1 cards in M0-M3 done;
- backend tests pass;
- Obsidian plugin builds;
- frontend builds if SPA files changed;
- seed thesis tree present;
- operator can enqueue thesis research from UI/API;
- no non-local infrastructure introduced.
```

The portal should show release readiness as a projection, not a separate manually maintained doc. A release gate reads card status, evidence, and open decisions.

## Safety

Automation must be conservative:

- no arbitrary command execution from imported JSON;
- no git push;
- no destructive git commands;
- no hidden mutation of design docs;
- command runner local-only;
- every generated card is reviewable before it affects work.

The portal directs implementation. It does not take ownership away from the operator.
