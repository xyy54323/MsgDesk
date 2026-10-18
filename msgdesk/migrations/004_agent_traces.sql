CREATE TABLE IF NOT EXISTS agent_traces (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    kind TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_traces_created
    ON agent_traces(created_at);
