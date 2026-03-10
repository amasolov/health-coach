"""Baseline: consolidated schema from db/migrations/001–011.

For existing databases (which already have these tables), the migration
runner stamps this revision without executing any SQL.  For fresh databases,
the full schema is applied.

Revision ID: 0001
Revises: None
Create Date: 2026-03-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Combined schema from 001_initial through 011_users_credentials.
# Every statement uses IF NOT EXISTS / IF NOT EXISTS so the migration
# is idempotent even if run on an already-provisioned database.

UPGRADE_SQL = """
-- == 001_initial ==

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS athlete_profile (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    effective_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    max_hr          SMALLINT,
    resting_hr      SMALLINT,
    lthr_run        SMALLINT,
    lthr_bike       SMALLINT,
    ftp_watts       SMALLINT,
    critical_power  SMALLINT,
    threshold_pace  NUMERIC(5,2),
    vo2max          NUMERIC(4,1),
    weight_kg       NUMERIC(5,2),
    height_cm       SMALLINT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, effective_date)
);

CREATE TABLE IF NOT EXISTS training_zones (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    effective_date  DATE NOT NULL,
    zone_type       TEXT NOT NULL,
    zone_number     SMALLINT NOT NULL,
    zone_name       TEXT NOT NULL,
    lower_bound     NUMERIC(8,2),
    upper_bound     NUMERIC(8,2),
    unit            TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, effective_date, zone_type, zone_number)
);

CREATE TABLE IF NOT EXISTS activities (
    time                TIMESTAMPTZ NOT NULL,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    source              TEXT NOT NULL DEFAULT 'garmin',
    source_id           TEXT,
    activity_type       TEXT NOT NULL,
    title               TEXT,
    duration_s          INTEGER,
    distance_m          NUMERIC(10,1),
    elevation_gain_m    NUMERIC(7,1),
    avg_hr              SMALLINT,
    max_hr              SMALLINT,
    avg_power           SMALLINT,
    max_power           SMALLINT,
    normalized_power    SMALLINT,
    tss                 NUMERIC(6,1),
    intensity_factor    NUMERIC(4,3),
    variability_index   NUMERIC(4,3),
    avg_cadence         SMALLINT,
    avg_pace_sec_km     NUMERIC(6,1),
    calories            INTEGER,
    training_effect_ae  NUMERIC(3,1),
    training_effect_an  NUMERIC(3,1),
    notes               TEXT,
    raw_data            JSONB
);
SELECT create_hypertable('activities', 'time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS body_composition (
    time            TIMESTAMPTZ NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    weight_kg       NUMERIC(5,2),
    body_fat_pct    NUMERIC(4,1),
    muscle_mass_kg  NUMERIC(5,2),
    bone_mass_kg    NUMERIC(4,2),
    bmi             NUMERIC(4,1),
    body_water_pct  NUMERIC(4,1),
    source          TEXT DEFAULT 'garmin_scale'
);
SELECT create_hypertable('body_composition', 'time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS vitals (
    time                TIMESTAMPTZ NOT NULL,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    resting_hr          SMALLINT,
    hrv_ms              NUMERIC(5,1),
    bp_systolic         SMALLINT,
    bp_diastolic        SMALLINT,
    bp_pulse            SMALLINT,
    sleep_score         SMALLINT,
    sleep_duration_min  SMALLINT,
    stress_avg          SMALLINT,
    body_battery_high   SMALLINT,
    body_battery_low    SMALLINT,
    spo2_avg            NUMERIC(4,1),
    respiration_avg     NUMERIC(4,1),
    source              TEXT DEFAULT 'garmin'
);
SELECT create_hypertable('vitals', 'time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS strength_sets (
    time            TIMESTAMPTZ NOT NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    workout_id      TEXT,
    exercise_name   TEXT NOT NULL,
    exercise_type   TEXT,
    muscle_group    TEXT,
    set_number      SMALLINT,
    set_type        TEXT DEFAULT 'normal',
    weight_kg       NUMERIC(6,2),
    reps            SMALLINT,
    rpe             NUMERIC(3,1),
    duration_s      INTEGER,
    distance_m      NUMERIC(8,1),
    notes           TEXT
);
SELECT create_hypertable('strength_sets', 'time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS training_load (
    time    TIMESTAMPTZ NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    tss     NUMERIC(6,1),
    ctl     NUMERIC(6,1),
    atl     NUMERIC(6,1),
    tsb     NUMERIC(6,1),
    ramp    NUMERIC(5,1),
    source  TEXT DEFAULT 'calculated'
);
SELECT create_hypertable('training_load', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_activities_user_type ON activities (user_id, activity_type, time DESC);
CREATE INDEX IF NOT EXISTS idx_activities_source ON activities (user_id, source, source_id);
CREATE INDEX IF NOT EXISTS idx_body_comp_user ON body_composition (user_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_vitals_user ON vitals (user_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_strength_user_exercise ON strength_sets (user_id, exercise_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_training_load_user ON training_load (user_id, time DESC);

-- == 002_pmc_projection ==

ALTER TABLE training_load ADD COLUMN IF NOT EXISTS projected BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_training_load_projected ON training_load (user_id, projected, time DESC);

-- == 004_strength_routine_id ==

ALTER TABLE strength_sets ADD COLUMN IF NOT EXISTS routine_id TEXT;
CREATE INDEX IF NOT EXISTS idx_strength_routine ON strength_sets (user_id, routine_id) WHERE routine_id IS NOT NULL;

-- == 005_ops_log ==

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

-- == 006_telegram_linking ==

ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT UNIQUE;
CREATE TABLE IF NOT EXISTS telegram_link_codes (
    code        TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- == 007_knowledge_base ==

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER REFERENCES users(id),
    filename        TEXT NOT NULL,
    title           TEXT,
    sha256          TEXT NOT NULL,
    page_count      INTEGER,
    chunk_count     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (sha256, user_id)
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    page_number     INTEGER,
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS knowledge_chunks_document_idx ON knowledge_chunks (document_id);

-- == 008_telegram_messages ==

CREATE TABLE IF NOT EXISTS telegram_messages (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    chat_id     BIGINT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS telegram_messages_user_recent ON telegram_messages (user_id, created_at DESC);

-- == 009_athlete_config ==

CREATE TABLE IF NOT EXISTS athlete_config (
    slug        TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id),
    config      JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS athlete_config_user_id ON athlete_config (user_id) WHERE user_id IS NOT NULL;

-- == 010_body_battery_latest ==

ALTER TABLE vitals ADD COLUMN IF NOT EXISTS body_battery_latest SMALLINT;
CREATE UNIQUE INDEX IF NOT EXISTS vitals_user_time_uniq ON vitals (user_id, "time");

-- == 011_users_credentials ==

ALTER TABLE users ADD COLUMN IF NOT EXISTS email              TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name         TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name          TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_email       TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_password    TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS hevy_api_key       TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS mcp_api_key        TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_uniq ON users (email) WHERE email != '';
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Downgrading past the baseline is not supported. "
        "Restore from a database backup instead."
    )
