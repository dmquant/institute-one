# Roadmap Card Template

```yaml
id:
title:
type: feature
phase:
status: inbox
priority: P1
risk: medium
owner:
tags: []
design_links: []
expected_files: []
dependencies: []
```

## Summary

One short paragraph describing the work.

## Problem

What gap does this close? Why does it matter now?

## Implementation Notes

Concrete files, modules, data shapes, UI components, and edge cases.

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Verification

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall app -q
```

## Agent Prompt

```text
Implement this card in institute-one.
Follow the design links and expected files.
Keep the change scoped.
Run verification commands and summarize results.
```
