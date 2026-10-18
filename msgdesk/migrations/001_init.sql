PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    account_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    secret_ref TEXT,
    default_category TEXT,
    default_priority TEXT NOT NULL DEFAULT 'normal',
    interval_seconds INTEGER NOT NULL DEFAULT 600,
    backfill_since TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error_code TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sources_enabled ON sources(enabled);
CREATE INDEX IF NOT EXISTS ix_sources_type ON sources(type);
CREATE INDEX IF NOT EXISTS ix_sources_account ON sources(account_id);
CREATE INDEX IF NOT EXISTS ix_sources_last_success ON sources(last_success_at);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    display_name TEXT NOT NULL,
    session_ref TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    capability_json TEXT NOT NULL DEFAULT '{}',
    client_version TEXT,
    adapter_version TEXT,
    last_connected_at TEXT,
    last_checked_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    cursor_type TEXT NOT NULL DEFAULT 'mixed',
    cursor_value TEXT,
    last_message_time TEXT,
    last_platform_message_id TEXT,
    etag TEXT,
    last_modified TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    platform_conversation_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'group',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversations_platform
    ON conversations(source_id, platform_conversation_id)
    WHERE platform_conversation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    account_id TEXT,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    sender_id TEXT,
    sender_name TEXT,
    platform_message_id TEXT,
    published_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    content_text TEXT,
    content_completeness TEXT NOT NULL DEFAULT 'complete',
    source_url TEXT,
    raw_payload_ref TEXT,
    content_hash TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'pending',
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_messages_source_time ON messages(source_id, published_at);
CREATE INDEX IF NOT EXISTS ix_messages_conversation_time ON messages(conversation_id, published_at);
CREATE INDEX IF NOT EXISTS ix_messages_status ON messages(processing_status);
CREATE INDEX IF NOT EXISTS ix_messages_hash ON messages(content_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_platform
    ON messages(source_id, conversation_id, platform_message_id)
    WHERE platform_message_id IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS message_sources (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_message_id TEXT,
    source_url TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, source_id)
);

CREATE TABLE IF NOT EXISTS message_processing (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    processor_version TEXT NOT NULL,
    rule_version TEXT,
    model_provider TEXT,
    model_name TEXT,
    category TEXT,
    subcategory TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    summary_short TEXT,
    summary_long TEXT,
    facts_json TEXT NOT NULL DEFAULT '[]',
    action_items_json TEXT NOT NULL DEFAULT '[]',
    deadline_at TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    urgency TEXT NOT NULL DEFAULT 'normal',
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'success',
    error_code TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_processing_message ON message_processing(message_id, is_current);
CREATE UNIQUE INDEX IF NOT EXISTS ux_processing_current
    ON message_processing(message_id)
    WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    source_id TEXT,
    message_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    error_message TEXT,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_jobs_dispatch ON jobs(status, available_at, priority);
CREATE INDEX IF NOT EXISTS ix_jobs_source ON jobs(source_id, status);
CREATE INDEX IF NOT EXISTS ix_jobs_trace ON jobs(trace_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_active_message_job
    ON jobs(type, message_id)
    WHERE message_id IS NOT NULL AND status IN ('pending', 'running', 'retry_waiting');

CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    content_hash TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    urgent_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error_code TEXT,
    generated_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_records (
    id TEXT PRIMARY KEY,
    message_id TEXT,
    digest_date TEXT,
    channel TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_dedupe
    ON notification_records(channel, dedupe_key);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL DEFAULT 'global',
    scope_id TEXT,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    condition_json TEXT NOT NULL DEFAULT '{}',
    action_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_rules_scope ON rules(scope_type, scope_id, enabled);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    message_id TEXT,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    input_chars INTEGER NOT NULL DEFAULT 0,
    output_chars INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL
);

