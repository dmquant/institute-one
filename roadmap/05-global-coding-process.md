# 05 - Global Coding Process

## Core Statement

`roadmap/` is the global coding process for this repository. The Kanban board is one projection. The source object is the implementation loop:

```text
design -> card -> session -> diff -> verification -> review -> release -> learning
```

Every substantial change should be traceable through that loop.

## Required Lifecycle

### 1. Design Intake

Inputs:

- design docs in `design/local-thesis-alpha/`;
- existing `ROADMAP.md`;
- bugs found during testing;
- operator requests;
- system-generated actions.

Output:

- roadmap card in `Inbox` or `Ready`;
- design links attached;
- decision gaps recorded.

### 2. Card Definition

A card becomes `Ready` only when it has:

- clear scope;
- design links;
- expected files or modules;
- dependencies;
- acceptance criteria;
- verification commands;
- priority and risk.

Cards without those fields remain in `Inbox`.

### 3. Coding Session

Implementation starts by claiming a card and opening a coding session.

Session rules:

- one session has one goal;
- planned files are declared before edits when possible;
- commands run are recorded;
- partial work is allowed but must be summarized;
- blockers create decisions or follow-up cards.

The session is the durable equivalent of "what happened in this coding turn."

### 4. Diff And Evidence

The session must produce evidence:

- changed file list;
- test output;
- build output;
- screenshots for UI;
- migration verification for schema;
- operator approval for decisions.

Evidence can be manual at first. Later the portal can collect it automatically.

### 5. Review

A card enters `Review` when implementation appears complete. Review checks:

- scope did not drift;
- design contract is met;
- unrelated files were not churned;
- tests are meaningful;
- new risks are recorded;
- follow-up work is captured.

### 6. Verify

`Verify` is the end-to-end proof column. For backend-only cards, targeted tests may be enough. For UI cards, use screenshots or browser checks. For data/alpha cards, use fixture data and deterministic outputs.

### 7. Done

Done requires:

- all acceptance criteria checked;
- required evidence attached;
- no unresolved blocker;
- release gate updated if applicable.

If a regression appears later, create a new card linked to the original. Do not rewrite history.

## Coding Process Rules

- No non-trivial implementation without a card.
- No card in progress without a session.
- No done without evidence.
- No schema change without migration and test.
- No UI change without build and visual verification when practical.
- No hosted infrastructure unless a design decision explicitly changes the local-only rule.
- No git push from the automation process.

## Release Gates

A release gate is a projection over cards and evidence.

Example:

```text
Release A: Thesis Registry + Forecastable Research
requires:
  M0 done
  M1 done
  M2 done
  M3 minimum done
  backend tests pass
  Obsidian plugin builds
  frontend builds if SPA files changed
  operator can enqueue thesis research
```

The portal should calculate readiness rather than requiring a manually updated status paragraph.

## Learning Loop

After each milestone:

1. summarize what took longer than expected;
2. file cards for repeated friction;
3. update card templates or verification commands;
4. update design docs only when architecture changed;
5. keep the process lightweight.

The roadmap process should improve the coding system the same way the thesis loop improves research.
