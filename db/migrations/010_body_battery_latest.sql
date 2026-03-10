ALTER TABLE vitals ADD COLUMN IF NOT EXISTS body_battery_latest SMALLINT;

CREATE UNIQUE INDEX IF NOT EXISTS vitals_user_time_uniq
    ON vitals (user_id, "time");
