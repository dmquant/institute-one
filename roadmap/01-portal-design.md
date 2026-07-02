# 01 - Portal Design

## Why An Embedded Portal

The thesis-alpha design is large enough that a Markdown checklist will rot. The implementation needs a living control surface for the global coding process. The board should show:

- what to build next;
- what is blocked;
- what design doc justifies the task;
- what files are expected to change;
- what tests prove completion;
- what human decision is required.

Embedding the portal inside the `institute-one` Obsidian plugin keeps project management close to the vault where design docs, research output, and implementation notes already live. The same local plugin that operates research should also direct the buildout of the research system. The portal is not merely a viewable surface; it is the operator UI for a durable coding process.

## User

Primary user: the operator/developer using AI coding agents.

Secondary users:

- future contributors reading project state;
- AI agents retrieving card context;
- the system itself, when it files implementation actions from failures or roadmap gaps.

## Navigation

Add a top-level Obsidian plugin view:

```text
institute-roadmap
```

Suggested command/ribbon label:

```text
路线图 Roadmap
```

Sub-views:

| View | Purpose |
|---|---|
| Board | Kanban columns and swimlanes. |
| Backlog | Unscheduled cards, filters, bulk import. |
| Milestones | M0-M10 implementation phases. |
| Releases | Release gates and verification bundles. |
| Decisions | Open operator decisions that block work. |
| Sessions | Active and historical coding sessions tied to cards. |
| Evidence | Verification, screenshots, diffs, and approvals. |

## Kanban Columns

Use workflow columns that match implementation reality:

| Column | Meaning | Entry rule | Exit rule |
|---|---|---|---|
| `Inbox` | Captured but not triaged. | New import or generated card. | Scope clarified and phase assigned. |
| `Ready` | Defined and unblocked. | Acceptance checks and dependencies known. | Work starts. |
| `In Progress` | Actively being implemented. | One owner/agent has claimed it. | Diff and verification available. |
| `Review` | Needs human/code review. | Implementation complete, tests run. | Approved or sent back. |
| `Verify` | Needs end-to-end proof. | Review passed. | Required command/screenshot/data checks pass. |
| `Done` | Complete. | Evidence attached. | Reopen only by explicit regression. |
| `Parked` | Deferred or intentionally blocked. | Out of scope, waiting decision, or dependency too large. | Operator reactivates. |

Do not allow a card to move to `Done` without at least one evidence item.

## Swimlanes

Use milestone swimlanes by default:

```text
M0 Research Hand Policy
M1 Thesis Registry
M2 Securities & Stock Map
M3 Thesis-Aware Research Queue
M4 Market Data & PIT Store
M5 Forecast Ledger
M6 Alpha & Paper Book
M7 Operator UI
M8 Vault Projection
M9 Sentinel & Review Automation
M10 Quality, Correction & Source Trust
```

Alternate swimlanes:

- domain: backend, frontend, data, workflows, vault, tests, docs;
- risk: high, medium, low;
- dependency: foundation, application, polish.

## Card Anatomy

Each card should show compact fields on the board:

```text
ID, title, phase, status, priority, risk, owner, blocked flag, acceptance count, test status
```

Card detail drawer:

- summary;
- problem statement;
- design links;
- expected files/modules;
- dependencies;
- implementation notes;
- acceptance criteria;
- verification commands;
- agent prompt;
- decisions;
- activity/events;
- evidence attachments.

## Coding Sessions

A card can have many coding sessions. A session is one concrete attempt by a human or agent to implement the card.

Session fields:

- session id;
- card id;
- actor: human, codex, claude, gemini, or other;
- goal;
- start/end time;
- planned files;
- touched files;
- commands run;
- result: completed, partial, blocked, reverted;
- summary;
- follow-up cards.

The portal should make active sessions visible. A card in `In Progress` without an active or recent session is stale.

## Status Rules

The portal should enforce lightweight rules:

- `Ready` requires acceptance criteria.
- `In Progress` requires owner.
- `Review` requires implementation note or linked diff.
- `Verify` requires at least one verification command.
- `Done` requires passing evidence or explicit operator override.
- blocked cards display the blocking dependency or decision.
- `In Progress` should create or attach a coding session.
- `Review` requires a session summary.
- `Verify` requires evidence linked to the session or card.

## Card Types

| Type | Meaning |
|---|---|
| `feature` | New user/system capability. |
| `schema` | Migration or durable data shape. |
| `workflow` | Prompt/workflow behavior. |
| `ui` | Obsidian plugin/operator surface. |
| `test` | Test coverage or verification harness. |
| `docs` | Documentation/design update. |
| `ops` | Scripts, service, maintenance, config. |
| `decision` | Human choice required before implementation. |

## Priority

Use explicit priorities:

| Priority | Meaning |
|---|---|
| `P0` | Blocks all meaningful progress or protects data correctness. |
| `P1` | Required for first useful thesis-alpha release. |
| `P2` | Important but can follow the first usable release. |
| `P3` | Nice-to-have or polish. |

## Board Interactions

Minimum:

- drag card across columns;
- filter by phase/type/status/priority;
- search title/body;
- open card detail;
- export a markdown-backed Kanban note compatible with existing Obsidian Kanban plugins;
- add checklist item;
- add dependency;
- mark blocked/unblocked;
- run or record verification command manually;
- export board JSON.

Later:

- generate agent prompt from card;
- start a local coding session from a card;
- attach git diff summary;
- auto-detect test results;
- file follow-up cards from failures.

## Embedded Portal And Research System

The roadmap portal should eventually feed the same operator-action loop as the research system. The first implementation can persist local plugin status overrides and export a Kanban-compatible note; the durable version stores cards, sessions, evidence, and events in the local backend. Examples:

- test failure creates roadmap card;
- data-health issue creates implementation/ops card;
- repeated model parse failure creates workflow-improvement card;
- missing UI surface from design creates backlog card.

That makes the portal the implementation control plane for the institute itself. Research loops improve investment theses; roadmap loops improve the codebase.
