#!/usr/bin/env python3
"""
Performance Management Chart (PMC) calculator.

Computes CTL (Chronic Training Load), ATL (Acute Training Load), and
TSB (Training Stress Balance) from daily TSS values per user.

Also generates 8-week projections based on historical training averages
or planned workout TSS (when available).

Usage:
    python scripts/calc_pmc.py
    # or via task:
    task pmc:calculate
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import psycopg2

from scripts.tz import load_user_tz, tz_date_cast, user_today
from scripts import ops_emit

CTL_TIME_CONSTANT = 42
ATL_TIME_CONSTANT = 7
PROJECTION_WEEKS = 8


def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_users(cur) -> list[tuple[int, str]]:
    cur.execute("SELECT id, slug FROM users ORDER BY id")
    return cur.fetchall()


def get_daily_tss(cur, user_id: int, tz_name: str) -> dict[date, float]:
    """Sum TSS per day for a user in their local timezone."""
    date_expr = f"(time AT TIME ZONE '{tz_name}')::date"
    cur.execute(f"""
        SELECT {date_expr} AS day, COALESCE(SUM(tss), 0) AS daily_tss
        FROM activities
        WHERE user_id = %s AND tss IS NOT NULL
        GROUP BY day
        ORDER BY day
    """, (user_id,))
    return {row[0]: float(row[1]) for row in cur.fetchall()}


def compute_pmc(daily_tss: dict[date, float], today: date) -> list[dict]:
    """Compute CTL/ATL/TSB for each day from the first activity to today."""
    if not daily_tss:
        return []

    start = min(daily_tss.keys())
    end = today

    ctl = 0.0
    atl = 0.0
    results = []

    current = start
    while current <= end:
        tss = daily_tss.get(current, 0.0)
        ctl = ctl + (tss - ctl) / CTL_TIME_CONSTANT
        atl = atl + (tss - atl) / ATL_TIME_CONSTANT
        tsb = ctl - atl

        results.append({
            "date": current,
            "tss": tss,
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
            "source": "calculated",
        })
        current += timedelta(days=1)

    return results


def compute_ramp(results: list[dict]) -> None:
    """Add week-over-week CTL ramp rate to results."""
    for i, r in enumerate(results):
        if i >= 7:
            r["ramp"] = round(r["ctl"] - results[i - 7]["ctl"], 1)
        else:
            r["ramp"] = 0.0


def project_future(results: list[dict], weeks: int = PROJECTION_WEEKS) -> list[dict]:
    """
    Project CTL/ATL/TSB forward using average daily TSS from the last 4 weeks.
    Returns projected rows.
    """
    if not results:
        return []

    last_28 = results[-28:] if len(results) >= 28 else results
    avg_daily_tss = sum(r["tss"] for r in last_28) / len(last_28)

    last = results[-1]
    ctl = last["ctl"]
    atl = last["atl"]

    projections = []
    current = last["date"] + timedelta(days=1)
    end = current + timedelta(weeks=weeks)

    while current <= end:
        ctl = ctl + (avg_daily_tss - ctl) / CTL_TIME_CONSTANT
        atl = atl + (avg_daily_tss - atl) / ATL_TIME_CONSTANT
        tsb = ctl - atl

        projections.append({
            "date": current,
            "tss": round(avg_daily_tss, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
            "ramp": 0.0,
            "source": "projected",
        })
        current += timedelta(days=1)

    return projections


def write_results(cur, user_id: int, results: list[dict]) -> None:
    """Upsert PMC results into training_load table."""
    cur.execute(
        "DELETE FROM training_load WHERE user_id = %s",
        (user_id,),
    )

    for r in results:
        cur.execute("""
            INSERT INTO training_load (time, user_id, tss, ctl, atl, tsb, ramp, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.combine(r["date"], datetime.min.time(), tzinfo=timezone.utc),
            user_id,
            r["tss"],
            r["ctl"],
            r["atl"],
            r["tsb"],
            r.get("ramp", 0.0),
            r["source"],
        ))


def main() -> int:
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    users = get_users(cur)
    if not users:
        print("No users found in database.")
        return 1

    for user_id, slug in users:
        print(f"\n--- PMC for {slug} ---")

        tz = load_user_tz(slug)
        today = user_today(tz)
        tz_name = str(tz)

        with ops_emit.timed("sync", "pmc_calc", user_id=user_id) as ctx:
            daily_tss = get_daily_tss(cur, user_id, tz_name)
            if not daily_tss:
                print(f"  No activities with TSS found for {slug}")
                ctx["rows"] = 0
                continue

            results = compute_pmc(daily_tss, today)
            compute_ramp(results)
            projections = project_future(results)

            all_rows = results + projections
            write_results(cur, user_id, all_rows)
            ctx["rows"] = len(all_rows)

        latest = results[-1] if results else None
        if latest:
            print(f"  CTL={latest['ctl']} ATL={latest['atl']} TSB={latest['tsb']}")
            print(f"  Days: {len(results)} historical + {len(projections)} projected")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
