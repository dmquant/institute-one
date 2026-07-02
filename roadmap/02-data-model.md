# 02 - Data Model

## Storage Strategy

Roadmap state should live in SQLite, not only JSON. `roadmap/backlog.json` is a seed/import/export artifact. The portal's live state belongs in database tables so it can be queried, updated through API routes, and connected to events.

## Core Tables

```sql
CREATE TABLE roadmap_cards (
  id                  TEXT PRIMARY KEY,
  title               TEXT NOT NULL,
  type                TEXT NOT NULL,
  phase               TEXT NOT NULL,
  status              TEXT NOT NULL,
  priority            TEXT NOT NULL,
  risk                TEXT NOT NULL DEFAULT 'medium',
  owner               TEXT,
  summary             TEXT NOT NULL DEFAULT '',
  problem             TEXT NOT NULL DEFAULT '',
  implementation      TEXT NOT NULL DEFAULT '',
  agent_prompt        TEXT NOT NULL DEFAULT '',
  design_links_json   TEXT NOT NULL DEFAULT '[]',
  expected_files_json TEXT NOT NULL DEFAULT '[]',
  tags_json           TEXT NOT NULL DEFAULT '[]',
  blocked_reason      TEXT,
  sort_order          REAL NOT NULL DEFAULT 0,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  completed_at        TEXT
);

CREATE TABLE roadmap_checklists (
  id           TEXT PRIMARY KEY,
  card_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,       -- acceptance|implementation|review
  text         TEXT NOT NULL,
  checked      INTEGER NOT NULL DEFAULT 0,
  sort_order   REAL NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE roadmap_dependencies (
  id             TEXT PRIMARY KEY,
  card_id        TEXT NOT NULL,
  depends_on_id  TEXT NOT NULL,
  relation       TEXT NOT NULL DEFAULT 'blocks',
  created_at     TEXT NOT NULL
);

CREATE TABLE roadmap_evidence (
  id           TEXT PRIMARY KEY,
  card_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,       -- command|test|screenshot|diff|doc|operator
  title        TEXT NOT NULL,
  body         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL,       -- pass|fail|info|override
  artifact_ref TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE roadmap_events (
  id           TEXT PRIMARY KEY,
  card_id      TEXT,
  event_type   TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
```

## Coding Session Tables

The roadmap is the global coding process, so implementation attempts are first-class.

```sql
CREATE TABLE roadmap_coding_sessions (
  id                  TEXT PRIMARY KEY,
  card_id             TEXT NOT NULL,
  actor               TEXT NOT NULL,       -- human|codex|claude|gemini|other
  goal                TEXT NOT NULL,
  status              TEXT NOT NULL,       -- active|completed|partial|blocked|cancelled
  planned_files_json  TEXT NOT NULL DEFAULT '[]',
  touched_files_json  TEXT NOT NULL DEFAULT '[]',
  summary             TEXT NOT NULL DEFAULT '',
  started_at          TEXT NOT NULL,
  finished_at         TEXT
);

CREATE TABLE roadmap_session_commands (
  id             TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL,
  command_label  TEXT NOT NULL,
  command_text   TEXT NOT NULL,
  exit_code      INTEGER,
  output_excerpt TEXT,
  created_at     TEXT NOT NULL
);

CREATE TABLE roadmap_decisions (
  id             TEXT PRIMARY KEY,
  card_id        TEXT,
  title          TEXT NOT NULL,
  question       TEXT NOT NULL,
  options_json   TEXT NOT NULL DEFAULT '[]',
  decision       TEXT,
  status         TEXT NOT NULL DEFAULT 'open',
  created_at     TEXT NOT NULL,
  resolved_at    TEXT
);

CREATE TABLE roadmap_release_gates (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  scope_json      TEXT NOT NULL DEFAULT '[]',
  criteria_json   TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'open',
  evidence_json   TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

## Status Values

```text
inbox
ready
in_progress
review
verify
done
parked
```

The UI can display title-case labels, but the database should use stable lowercase ids.

## Seed Import

Seed file:

```text
roadmap/backlog.json
```

Import command/API:

```text
POST /api/roadmap/import
```

Rules:

- upsert by `id`;
- preserve local status unless import says `force`;
- merge checklist items by text;
- validate dependencies refer to known ids;
- reject unknown status/type/priority.

## API Routes

```text
GET    /api/roadmap/cards
GET    /api/roadmap/cards/{id}
POST   /api/roadmap/cards
PATCH  /api/roadmap/cards/{id}
POST   /api/roadmap/cards/{id}/move
POST   /api/roadmap/cards/{id}/claim
POST   /api/roadmap/cards/{id}/checklists
PATCH  /api/roadmap/checklists/{id}
POST   /api/roadmap/cards/{id}/dependencies
DELETE /api/roadmap/dependencies/{id}
POST   /api/roadmap/cards/{id}/evidence
POST   /api/roadmap/import
GET    /api/roadmap/export
POST   /api/roadmap/cards/{id}/sessions
PATCH  /api/roadmap/sessions/{id}
POST   /api/roadmap/sessions/{id}/commands
GET    /api/roadmap/sessions
POST   /api/roadmap/decisions
PATCH  /api/roadmap/decisions/{id}
GET    /api/roadmap/release-gates
```

## Move Semantics

`POST /move`:

```json
{
  "status": "review",
  "sort_order": 1000,
  "reason": "implementation complete; tests run"
}
```

Validation:

- cannot move to `ready` if acceptance checklist empty;
- cannot move to `in_progress` without owner unless owner is provided;
- cannot move to `done` without evidence unless `override=true`;
- cannot move blocked card forward unless override is explicit.

## Frontend Data Shape

```ts
type RoadmapCard = {
  id: string;
  title: string;
  type: CardType;
  phase: string;
  status: RoadmapStatus;
  priority: "P0" | "P1" | "P2" | "P3";
  risk: "low" | "medium" | "high";
  owner?: string;
  summary: string;
  problem: string;
  implementation: string;
  agent_prompt: string;
  design_links: string[];
  expected_files: string[];
  tags: string[];
  blocked_reason?: string;
  checklists: ChecklistItem[];
  dependencies: Dependency[];
  evidence: Evidence[];
};
```

## Event Integration

Every user-visible change should write a roadmap event:

- `card.created`;
- `card.updated`;
- `card.moved`;
- `card.claimed`;
- `checklist.checked`;
- `dependency.added`;
- `evidence.added`;
- `session.started`;
- `session.completed`;
- `decision.opened`;
- `decision.resolved`;
- `release_gate.updated`;
- `import.completed`.

These events can later feed the existing SSE stream and vault/export projections.

## Vault Projection

Optional projection:

```text
Roadmap/
  Board.md
  M1-Thesis-Registry.md
  M2-Securities-And-Stock-Map.md
  Done.md
```

This projection is for reading. The database remains source of truth.
