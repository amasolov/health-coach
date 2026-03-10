-- Single-source-of-truth for per-user athlete configuration.
--
-- The config JSONB column stores per-user data: profile, thresholds,
-- body, goals, etc.

CREATE TABLE IF NOT EXISTS athlete_config (
    slug        TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    config      JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS athlete_config_user_id
    ON athlete_config (user_id) WHERE user_id IS NOT NULL;
