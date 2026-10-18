CREATE TABLE IF NOT EXISTS conversation_digests (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT,
    date_text TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    summary_md TEXT NOT NULL DEFAULT '',
    model_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, conversation_id, date_text)
);
