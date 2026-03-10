CREATE TABLE IF NOT EXISTS ops_log (
    time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category    TEXT NOT NULL,
    event       TEXT NOT NULL,
    user_id     INTEGER,
    status      TEXT DEFAULT 'ok',
    duration_ms INTEGER,
    detail      JSONB DEFAULT '{}'
);
SELECT create_hypertable('ops_log', 'time', if_not_exists => TRUE);
