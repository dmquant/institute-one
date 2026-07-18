-- Card M7-008: operator-added dependencies must survive seed re-imports.
-- Import reconciliation only owns rows it wrote itself (source='import');
-- rows added through the API carry source='manual' and are never reconciled.
ALTER TABLE roadmap_dependencies ADD COLUMN source TEXT NOT NULL DEFAULT 'import';
