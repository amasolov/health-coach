-- Initial schema for health & fitness tracking
-- Requires TimescaleDB extension
-- Multi-user: all data tables reference users(id)

CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ============================================================================
-- Users
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- Athlete Profile (semi-static, versioned per user)
-- ============================================================================
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
    threshold_pace  NUMERIC(5,2),       -- min/km
    vo2max          NUMERIC(4,1),
    weight_kg       NUMERIC(5,2),
    height_cm       SMALLINT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, effective_date)
);

-- ============================================================================
-- Training Zones (versioned by effective_date, per user)
-- ============================================================================
CREATE TABLE IF NOT EXISTS training_zones (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    effective_date  DATE NOT NULL,
    zone_type       TEXT NOT NULL,       -- hr, running_power, cycling_power, pace
    zone_number     SMALLINT NOT NULL,
    zone_name       TEXT NOT NULL,
    lower_bound     NUMERIC(8,2),
    upper_bound     NUMERIC(8,2),
    unit            TEXT NOT NULL,       -- bpm, watts, min/km
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, effective_date, zone_type, zone_number)
);

-- ============================================================================
-- Activities (time-series, per user)
-- ============================================================================
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

-- ============================================================================
-- Body Composition (time-series, per user)
-- ============================================================================
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

-- ============================================================================
-- Vitals (time-series, daily, per user)
-- ============================================================================
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

-- ============================================================================
-- Strength Sets (from Hevy, per user)
-- ============================================================================
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

-- ============================================================================
-- Training Load (daily CTL/ATL/TSB, per user)
-- ============================================================================
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

-- ============================================================================
-- Indexes (user_id as leading column for efficient per-user queries)
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_activities_user_type ON activities (user_id, activity_type, time DESC);
CREATE INDEX IF NOT EXISTS idx_activities_source ON activities (user_id, source, source_id);
CREATE INDEX IF NOT EXISTS idx_body_comp_user ON body_composition (user_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_vitals_user ON vitals (user_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_strength_user_exercise ON strength_sets (user_id, exercise_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_training_load_user ON training_load (user_id, time DESC);
