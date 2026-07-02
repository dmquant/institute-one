# 06 - Agent Protocol

## Purpose

This project is built with AI coding agents. The roadmap control plane should make agent work repeatable, reviewable, and tied to implementation state.

## Agent Inputs

An agent should receive:

- card id and title;
- summary/problem;
- design links;
- expected files;
- dependencies;
- acceptance criteria;
- verification commands;
- current git status summary;
- constraints such as no push and no hosted infrastructure.

## Agent Output Contract

At the end of a coding session, the agent should report:

- files changed;
- what was implemented;
- verification commands and results;
- unresolved risks;
- follow-up cards needed;
- whether the card is ready for review or still partial.

This output becomes the session summary and evidence.

## Prompt Template

```text
You are implementing roadmap card ${CARD_ID}: ${TITLE}.

Design links:
${DESIGN_LINKS}

Expected files:
${EXPECTED_FILES}

Acceptance criteria:
${ACCEPTANCE}

Verification:
${VERIFY_COMMANDS}

Constraints:
- keep changes scoped to this card;
- do not introduce hosted infrastructure;
- do not push;
- preserve user changes;
- add or update tests according to risk;
- record follow-up work as roadmap cards rather than silently expanding scope.

Implement the card, run verification, and summarize changed files and results.
```

## Agent Guardrails

- If the card is underspecified, the agent should improve the card or open a decision, not guess blindly.
- If dependencies are not done, the agent should stop or implement only the dependency card.
- If tests fail outside scope, record evidence and decide whether to fix or file a follow-up.
- If a change requires destructive git commands, stop and ask.
- If implementation reveals a design flaw, update design through a dedicated docs card or decision.

## Multi-Agent Work

Multiple agents can work in parallel only when cards have no overlapping expected files and no direct dependency edge.

The portal should warn on:

- same file planned by two active sessions;
- dependency card not done;
- one card touching another card's claimed module;
- too many in-progress cards in one phase.

## Agent Completion States

| State | Meaning |
|---|---|
| `completed` | Acceptance criteria met and verification passed. |
| `partial` | Useful work landed but card remains open. |
| `blocked` | Cannot proceed without decision/dependency. |
| `cancelled` | Session stopped without durable work. |

The card status should not automatically become `Done` just because an agent says completed. Completion moves the card to `Review` or `Verify`; evidence and operator review decide final status.
