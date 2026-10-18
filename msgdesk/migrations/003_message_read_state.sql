ALTER TABLE messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_messages_read
    ON messages(is_read, published_at);
