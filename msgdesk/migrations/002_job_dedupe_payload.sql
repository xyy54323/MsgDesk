PRAGMA foreign_keys = ON;

DROP INDEX IF EXISTS ux_jobs_active_message_job;

CREATE UNIQUE INDEX ux_jobs_active_message_job
    ON jobs(type, message_id, payload_json)
    WHERE message_id IS NOT NULL
      AND status IN ('pending', 'running', 'retry_waiting');

