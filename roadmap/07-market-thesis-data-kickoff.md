# 07 - Market Thesis Data Kickoff

## New Kickoff Decision

Use `market-thesis-data/` as the initial implementation base.

This changes the first coding target from "create an empty thesis registry and seed AI manually" to:

```text
import a real exported thesis/lane/stock/edge bundle into the local thesis-alpha schema
```

## Bundle Summary

From `market-thesis-data/manifest.json`:

- schema: `researchos.market_thesis_export.manifest.v1`;
- generated at: 2026-07-01T01:45:20.376Z;
- source generated at: 2026-06-30T13:20:12.832Z;
- source date range: 2026-04-23 to 2026-06-30;
- theses: 74;
- lanes: 55;
- stocks / ETFs: 236;
- graph edges: 1,888;
- thesis-stock edges: 1,020.

Market distribution in `stocks.json`:

```text
US:            96
A-share:      75
HK:            26
US ETF:        21
US ADR:         8
A-share ETF:   5
Korea:          2
HK ETF:         2
Japan:          1
```

All 74 thesis directions are currently `conflicting`, which is useful: the import should treat them as hypotheses requiring validation, not conclusions.

## Implementation Impact

The first product milestone becomes:

```text
M1: import lanes, theses, securities, and thesis-security edges from market-thesis-data.
```

M1 should produce a local registry immediately populated with real market structure. M2 then hardens security normalization and edge editing. M3 runs thesis-aware research against imported theses.

## Import Cards

Recommended card order:

1. `M1-000` Document and validate market-thesis-data import contract.
2. `M1-001` Add thesis/import schema migration.
3. `M1-002` Implement thesis domain module and API.
4. `M2-001` Add/normalize security master schema.
5. `M1-003` Import market-thesis-data bundle.
6. `M3-001` Extend research queue for thesis-aware tasks.

## Import Acceptance

Minimum importer behavior:

- read `market-thesis-data/bundle.json`;
- validate schema and counts;
- import lanes as top-level thesis/lane objects;
- import theses under lanes;
- import stocks as securities;
- import thesis-stock edges;
- preserve practical metadata;
- record import provenance;
- support dry-run and apply;
- be idempotent.

## First Coding Session

Recommended first real product card after roadmap control-plane baseline:

```text
M1-001:
Add schema for theses plus market thesis import provenance, then write tests around a dry-run import of market-thesis-data.
```

Expected files:

```text
migrations/*.sql
app/institute/theses.py
app/institute/market_thesis_import.py
tests/test_market_thesis_import.py
```

Verification:

```bash
.venv/bin/python -m pytest tests/test_market_thesis_import.py -q
.venv/bin/python -m compileall app -q
```
