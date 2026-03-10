-- Migrate user credentials from users.json into the users table.
-- Idempotent: every statement uses IF NOT EXISTS / IF EXISTS guards.

ALTER TABLE users ADD COLUMN IF NOT EXISTS email              TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name         TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name          TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_email       TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_password    TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS hevy_api_key       TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS mcp_api_key        TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS users_email_uniq
    ON users (email) WHERE email != '';
