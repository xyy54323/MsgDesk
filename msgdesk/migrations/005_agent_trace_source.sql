ALTER TABLE agent_traces ADD COLUMN source_id TEXT;

UPDATE agent_traces
SET source_id = (
    SELECT id FROM sources WHERE deleted_at IS NULL ORDER BY created_at LIMIT 1
)
WHERE source_id IS NULL
  AND (SELECT COUNT(*) FROM sources WHERE deleted_at IS NULL) = 1;
