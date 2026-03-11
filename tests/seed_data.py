"""Synthetic test data for the ephemeral TimescaleDB container.

All dates are relative to today so time-sensitive assertions (e.g.
"at least one activity in last 30 days") always pass regardless of
when the suite is run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone


def seed(conn) -> int:
    """Insert synthetic rows into all tables tests depend on.

    Returns the auto-generated user id.
    """
    cur = conn.cursor()

    # ── user ──────────────────────────────────────────────────────
    cur.execute(
        """INSERT INTO users
               (slug, display_name, email, first_name, last_name,
                garmin_email, garmin_password, hevy_api_key, mcp_api_key,
                onboarding_complete)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
           RETURNING id""",
        (
            "testuser", "Test User", "test@example.com",
            "Test", "User",
            "test@garmin.example", "garmin-pass-placeholder",
            "hevy-key-placeholder", "mcp-key-placeholder",
        ),
    )
    user_id: int = cur.fetchone()[0]

    _seed_activities(cur, user_id)
    _seed_vitals(cur, user_id)
    _seed_body_composition(cur, user_id)
    _seed_training_load(cur, user_id)
    _seed_strength_sets(cur, user_id)
    _seed_athlete_config(cur, user_id)
    _seed_threshold_history(cur, user_id)

    conn.commit()
    return user_id


# ── helpers ───────────────────────────────────────────────────────

def _ts(days_ago: int, hour: int = 8) -> datetime:
    """Return a timezone-aware UTC timestamp *days_ago* from today."""
    d = date.today() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc)


def _seed_activities(cur, user_id: int) -> None:
    activities = [
        # (days_ago, type, title, duration_s, distance_m, elev, avg_hr, max_hr,
        #  avg_power, max_power, np, tss, IF, cadence, pace, cal, te_ae, te_an)
        (2, "running", "Easy Run", 2400, 5200, 30, 142, 158, None, None, None, 45.0, None, 170, 277, 380, 2.8, 0.5),
        (4, "cycling", "Zone 2 Ride", 3600, 25000, 150, 135, 155, 180, 320, 195, 65.0, 0.72, 85, None, 550, 2.5, 0.3),
        (6, "strength_training", "Upper Body", 2700, None, None, 115, 140, None, None, None, 40.0, None, None, None, 300, 1.8, 1.2),
        (9, "running", "Tempo Run", 3000, 7500, 45, 162, 178, None, None, None, 75.0, None, 175, 240, 520, 3.5, 1.8),
        (11, "cycling", "Sweet Spot", 4200, 32000, 200, 148, 170, 210, 380, 225, 90.0, 0.83, 88, None, 700, 3.2, 1.0),
        (14, "strength_training", "Lower Body", 3000, None, None, 120, 145, None, None, None, 45.0, None, None, None, 350, 2.0, 1.5),
        (17, "running", "Long Run", 5400, 13000, 80, 148, 165, None, None, None, 85.0, None, 168, 249, 750, 3.8, 0.8),
        (20, "cycling", "Recovery Spin", 2400, 18000, 50, 118, 135, 140, 220, 150, 30.0, 0.56, 82, None, 320, 1.5, 0.1),
        (23, "strength_training", "Full Body", 3300, None, None, 125, 150, None, None, None, 50.0, None, None, None, 380, 2.2, 1.4),
        (26, "running", "Intervals", 2700, 6000, 25, 168, 185, None, None, None, 80.0, None, 180, 270, 480, 4.0, 3.0),
        (30, "cycling", "Endurance", 5400, 40000, 300, 140, 160, 190, 350, 200, 100.0, 0.74, 86, None, 800, 3.0, 0.5),
        (35, "running", "Recovery Jog", 1800, 3500, 15, 130, 145, None, None, None, 25.0, None, 165, 308, 260, 2.0, 0.2),
        (42, "strength_training", "Push Day", 2700, None, None, 118, 142, None, None, None, 42.0, None, None, None, 310, 1.9, 1.3),
        (50, "running", "Fartlek", 3000, 7000, 35, 155, 175, None, None, None, 70.0, None, 172, 257, 500, 3.3, 2.0),
        (55, "cycling", "Hill Repeats", 3600, 28000, 400, 155, 180, 230, 450, 245, 95.0, 0.91, 80, None, 650, 3.8, 2.5),
    ]
    for row in activities:
        cur.execute(
            """INSERT INTO activities
                   (time, user_id, source, activity_type, title,
                    duration_s, distance_m, elevation_gain_m,
                    avg_hr, max_hr, avg_power, max_power, normalized_power,
                    tss, intensity_factor, avg_cadence, avg_pace_sec_km,
                    calories, training_effect_ae, training_effect_an)
               VALUES (%s,%s,'garmin',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (_ts(row[0]), user_id, *row[1:]),
        )


def _seed_vitals(cur, user_id: int) -> None:
    for days_ago in range(30):
        cur.execute(
            """INSERT INTO vitals
                   (time, user_id, resting_hr, hrv_ms,
                    sleep_score, sleep_duration_min, stress_avg,
                    body_battery_high, body_battery_low, body_battery_latest,
                    spo2_avg, respiration_avg)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                _ts(days_ago, hour=6),
                user_id,
                52 + (days_ago % 5),          # resting_hr 52-56
                45.0 + (days_ago % 8),         # hrv_ms 45-52
                75 + (days_ago % 15),          # sleep_score 75-89
                420 + (days_ago % 60),         # sleep_duration_min 420-479
                30 + (days_ago % 20),          # stress_avg 30-49
                85 + (days_ago % 10),          # body_battery_high 85-94
                25 + (days_ago % 15),          # body_battery_low 25-39
                60 + (days_ago % 20),          # body_battery_latest 60-79
                97.0 + (days_ago % 3) * 0.3,  # spo2_avg 97.0-97.6
                15.0 + (days_ago % 4) * 0.2,  # respiration_avg 15.0-15.6
            ),
        )


def _seed_body_composition(cur, user_id: int) -> None:
    for days_ago in range(0, 60, 2):
        cur.execute(
            """INSERT INTO body_composition
                   (time, user_id, weight_kg, body_fat_pct,
                    muscle_mass_kg, bone_mass_kg, bmi, body_water_pct)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                _ts(days_ago, hour=7),
                user_id,
                82.5 - days_ago * 0.02,  # slow downtrend
                18.0 + days_ago * 0.01,
                36.5,
                3.2,
                25.1 - days_ago * 0.005,
                55.0,
            ),
        )


def _seed_training_load(cur, user_id: int) -> None:
    ctl = 55.0
    atl = 60.0
    for days_ago in range(120, -1, -1):
        daily_tss = 40 + (days_ago % 7) * 10
        ctl = ctl + (daily_tss - ctl) / 42
        atl = atl + (daily_tss - atl) / 7
        tsb = round(ctl - atl, 1)
        ramp = round((ctl - (ctl - (daily_tss - ctl) / 42)) * 7, 1)
        cur.execute(
            """INSERT INTO training_load
                   (time, user_id, tss, ctl, atl, tsb, ramp, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'calculated')""",
            (
                _ts(days_ago, hour=23),
                user_id,
                round(daily_tss, 1),
                round(ctl, 1),
                round(atl, 1),
                tsb,
                ramp,
            ),
        )

    # A few projected rows for get_fitness_summary projection test
    for days_ahead in range(1, 15):
        proj_ctl = ctl + (40 - ctl) / 42 * days_ahead
        proj_atl = atl + (40 - atl) / 7 * days_ahead
        cur.execute(
            """INSERT INTO training_load
                   (time, user_id, tss, ctl, atl, tsb, ramp, source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'projected')""",
            (
                datetime(
                    *(date.today() + timedelta(days=days_ahead)).timetuple()[:3],
                    23, 0, 0, tzinfo=timezone.utc,
                ),
                user_id,
                0,
                round(proj_ctl, 1),
                round(proj_atl, 1),
                round(proj_ctl - proj_atl, 1),
                0,
            ),
        )


def _seed_strength_sets(cur, user_id: int) -> None:
    workouts = [
        # (days_ago, workout_id, routine_id, exercises)
        (6, "hevy-wkt-001", "ifit-routine-001", [
            ("Squat (Barbell)", "weight_reps", "quadriceps", [
                (1, "normal", 60, 10), (2, "normal", 80, 8), (3, "normal", 80, 8),
            ]),
            ("Romanian Deadlift (Dumbbell)", "weight_reps", "hamstrings", [
                (1, "normal", 20, 12), (2, "normal", 20, 12), (3, "normal", 20, 10),
            ]),
        ]),
        (14, "hevy-wkt-002", None, [
            ("Bench Press (Barbell)", "weight_reps", "chest", [
                (1, "warmup", 40, 10), (2, "normal", 60, 8),
                (3, "normal", 60, 8), (4, "normal", 60, 7),
            ]),
            ("Bicep Curl (Dumbbell)", "weight_reps", "biceps", [
                (1, "normal", 12, 12), (2, "normal", 12, 12), (3, "normal", 12, 10),
            ]),
        ]),
        (23, "hevy-wkt-003", None, [
            ("Squat (Barbell)", "weight_reps", "quadriceps", [
                (1, "normal", 60, 10), (2, "normal", 75, 8), (3, "normal", 75, 8),
            ]),
            ("Lateral Raise (Dumbbell)", "weight_reps", "shoulders", [
                (1, "normal", 8, 15), (2, "normal", 8, 15), (3, "normal", 8, 12),
            ]),
        ]),
    ]
    for days_ago, wkt_id, routine_id, exercises in workouts:
        ts = _ts(days_ago, hour=9)
        for ex_name, ex_type, muscle, sets in exercises:
            for set_num, set_type, weight, reps in sets:
                cur.execute(
                    """INSERT INTO strength_sets
                           (time, user_id, workout_id, exercise_name,
                            exercise_type, muscle_group, set_number,
                            set_type, weight_kg, reps, routine_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        ts, user_id, wkt_id, ex_name,
                        ex_type, muscle, set_num,
                        set_type, weight, reps, routine_id,
                    ),
                )


def _seed_athlete_config(cur, user_id: int) -> None:
    config = {
        "profile": {
            "name": "Test User",
            "date_of_birth": "1990-05-15",
            "sex": "male",
            "height_cm": 180,
            "timezone": "America/New_York",
        },
        "thresholds": {
            "last_tested": "2026-01-15",
            "heart_rate": {
                "max_hr": 190,
                "resting_hr": 52,
                "lthr_run": 168,
                "lthr_bike": 165,
            },
            "running": {
                "critical_power": 320,
                "threshold_pace": "4:30",
                "vo2max_garmin": 52.0,
                "vo2max_lab": None,
                "rftp_garmin": 310,
            },
            "cycling": {"ftp": 250, "ftp_wkg": 3.0},
            "lactate": {
                "lt1_hr": None, "lt1_pace": None,
                "lt2_hr": None, "lt2_pace": None,
                "test_protocol": None, "test_date": None,
            },
            "_sources": {},
        },
        "body": {
            "weight_kg": 82.5,
            "body_fat_pct": 18.0,
            "muscle_mass_kg": 36.5,
            "bone_mass_kg": 3.2,
            "bmi": 25.1,
            "measured_date": str(date.today()),
            "source": "garmin_scale",
        },
        "goals": {
            "primary_goal": "Improve overall fitness",
            "target_event": "Half Marathon",
            "target_date": "2026-09-15",
            "secondary_goals": ["Build strength", "Lose body fat"],
            "available_hours_per_week": 8,
            "preferred_sports": ["running", "cycling", "strength"],
            "constraints": ["Bad left knee"],
            "experience_level": "intermediate",
            "training_preferences": {
                "likes": "Varied workouts",
                "dislikes": "Long monotonous sessions",
            },
        },
        "training_status": {
            "weekly_volume_hrs": 6,
            "longest_run_km": 18,
            "longest_ride_km": 60,
            "strength_sessions_per_week": 2,
            "current_phase": "base",
        },
        "action_items": [],
        "ifit": {
            "favourite_trainers": ["John"],
            "available_equipment": ["treadmill", "dumbbells"],
            "preferred_duration_min": [20, 45],
            "min_rating": 4.0,
            "software_number": None,
        },
        "treadmill": {"zone_speed_map": {}, "hill_map": {}},
    }

    from psycopg2.extras import Json
    cur.execute(
        """INSERT INTO athlete_config (slug, user_id, config, updated_at)
           VALUES (%s, %s, %s, NOW())""",
        ("testuser", user_id, Json(config)),
    )


def _seed_threshold_history(cur, user_id: int) -> None:
    cur.execute(
        """INSERT INTO threshold_history
               (user_id, effective_date, ftp, rftp,
                lthr_run, lthr_bike, resting_hr, max_hr,
                weight_kg, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            user_id, date.today() - timedelta(days=30),
            250, 310,
            168, 165, 52, 190,
            82.5, "garmin",
        ),
    )
