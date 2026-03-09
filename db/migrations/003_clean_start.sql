-- Remove hardcoded dev user 'alexey' and all associated data.
-- Safe to run on any instance: if the user doesn't exist the DELETEs are no-ops.
DO $$
DECLARE
    v_user_id INTEGER;
BEGIN
    SELECT id INTO v_user_id FROM users WHERE slug = 'alexey';
    IF v_user_id IS NOT NULL THEN
        DELETE FROM training_load    WHERE user_id = v_user_id;
        DELETE FROM strength_sets    WHERE user_id = v_user_id;
        DELETE FROM vitals           WHERE user_id = v_user_id;
        DELETE FROM body_composition WHERE user_id = v_user_id;
        DELETE FROM activities       WHERE user_id = v_user_id;
        DELETE FROM training_zones   WHERE user_id = v_user_id;
        DELETE FROM athlete_profile  WHERE user_id = v_user_id;
        DELETE FROM users            WHERE id = v_user_id;
    END IF;
END $$;
