-- Add routine_id to strength_sets so completed Hevy workouts can be linked
-- back to the routine (and by extension, the iFit workout) they originated from.

ALTER TABLE strength_sets ADD COLUMN IF NOT EXISTS routine_id TEXT;

CREATE INDEX IF NOT EXISTS idx_strength_routine
    ON strength_sets (user_id, routine_id)
    WHERE routine_id IS NOT NULL;
