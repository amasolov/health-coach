-- Add projection tracking to training_load
-- The source column already distinguishes 'calculated' from 'projected';
-- this boolean index makes Grafana filtering efficient.

ALTER TABLE training_load ADD COLUMN IF NOT EXISTS
    projected BOOLEAN DEFAULT FALSE;

UPDATE training_load SET projected = TRUE WHERE source = 'projected';
UPDATE training_load SET projected = FALSE WHERE source != 'projected';

CREATE INDEX IF NOT EXISTS idx_training_load_projected
    ON training_load (user_id, projected, time DESC);
