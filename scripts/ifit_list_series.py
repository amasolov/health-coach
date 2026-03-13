#!/usr/bin/env python3
"""
List the full iFit workout library sorted by trainer name.

Fetches all trainers and all discoverable workouts from the iFit API,
caches them locally, and prints a summary grouped by trainer.

Usage:
    python scripts/ifit_list_series.py
    python scripts/ifit_list_series.py --type strength
    python scripts/ifit_list_series.py --type run
    python scripts/ifit_list_series.py --refresh   # force re-download
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict

import httpx

try:
    from scripts.ifit_auth import get_auth_headers
except ImportError:
    from ifit_auth import get_auth_headers

API = "https://api.ifit.com"
LYCAN = "https://gateway.ifit.com/lycan/v1"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".ifit_capture")
TRAINERS_CACHE = os.path.join(CACHE_DIR, "trainers.json")
WORKOUTS_CACHE = os.path.join(CACHE_DIR, "library_workouts.json")
CACHE_MAX_AGE = 86400 * 7  # 7 days

PAGE_SIZE = 80
CONCURRENCY = 6


def _cache_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    if (time.time() - os.path.getmtime(path)) >= CACHE_MAX_AGE:
        return False
    if path == WORKOUTS_CACHE:
        try:
            with open(path) as f:
                sample = json.load(f)
            if sample and isinstance(sample, list) and "description" not in sample[0]:
                print("  Cache missing 'description' field — needs refresh")
                return False
        except Exception:
            return False
    return True


def fetch_all_trainers(headers: dict) -> dict[str, dict]:
    """Fetch all trainers from the legacy API. Returns {id: trainer_dict}."""
    if _cache_fresh(TRAINERS_CACHE):
        with open(TRAINERS_CACHE) as f:
            return json.load(f)

    trainers: dict[str, dict] = {}
    page = 1
    with httpx.Client(timeout=15) as client:
        while True:
            r = client.get(
                f"{API}/v1/trainers?perPage=100&page={page}", headers=headers,
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            for t in batch:
                tid = t.get("_id") or t.get("id", "")
                trainers[tid] = {
                    "name": f"{t.get('first_name', '')} {t.get('last_name', '')}".strip(),
                    "title": t.get("title", ""),
                    "short_bio": t.get("short_bio", ""),
                }
            print(f"  Trainers page {page}: {len(batch)} (total {len(trainers)})")
            if len(batch) < 100:
                break
            page += 1

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(TRAINERS_CACHE, "w") as f:
        json.dump(trainers, f, indent=2)
    return trainers


async def _fetch_page(
    client: httpx.AsyncClient,
    headers: dict,
    page: int,
    sem: asyncio.Semaphore,
) -> list[dict]:
    async with sem:
        r = await client.get(
            f"{LYCAN}/workouts",
            params={"is_discoverable": "true", "perPage": PAGE_SIZE, "page": page},
            headers=headers,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []


def _extract_route_stats(controls: list[dict]) -> dict:
    """Compute incline and speed statistics from lycan workout controls.

    Controls are time-series data points with type "incline" (percentage)
    or "mps" (meters per second).  Returns averages and maxima for each.
    """
    inclines = [c["value"] for c in controls if c.get("type") == "incline"]
    speeds = [c["value"] for c in controls if c.get("type") == "mps"]

    return {
        "avg_incline_pct": round(sum(inclines) / len(inclines), 1) if inclines else 0,
        "max_incline_pct": max(inclines) if inclines else 0,
        "avg_speed_mps": round(sum(speeds) / len(speeds), 2) if speeds else 0,
        "max_speed_mps": max(speeds) if speeds else 0,
    }


def _slim_workout(w: dict) -> dict:
    """Extract only the fields we need to keep the cache manageable."""
    meta = w.get("metadata") or {}
    lib_filters = w.get("library_filters", [])
    estimates = w.get("estimates") or {}
    categories = set()
    subcategories = set()
    equipment_types = set()
    for entry in lib_filters:
        if isinstance(entry, dict):
            equipment_types.add(entry.get("equipment_type", ""))
            for cat in entry.get("categories", []):
                categories.add(cat.get("name", ""))
                for sc in cat.get("subcategories", []):
                    subcategories.add(sc)

    route_stats = _extract_route_stats(w.get("controls", []))
    loc_types = w.get("location_types", [])

    return {
        "id": w.get("id", ""),
        "title": w.get("title", ""),
        "description": w.get("description", ""),
        "type": w.get("type", ""),
        "trainer_id": meta.get("trainer", ""),
        "difficulty": w.get("difficulty", {}).get("rating", ""),
        "rating_avg": w.get("ratings", {}).get("average", 0),
        "rating_count": w.get("ratings", {}).get("count", 0),
        "time_sec": estimates.get("time", 0),
        "calories": estimates.get("calories", 0),
        "required_equipment": w.get("required_equipment", []),
        "categories": sorted(categories - {""}),
        "subcategories": sorted(subcategories - {""}),
        "equipment_types": sorted(equipment_types - {""}),
        "workout_group_id": w.get("workout_group_id"),
        "workout_filters": w.get("workout_filters", []),
        "distance_m": estimates.get("distance", 0) or 0,
        "elevation_gain_m": estimates.get("gross_elevation_gain", 0) or 0,
        "elevation_loss_m": estimates.get("gross_elevation_loss", 0) or 0,
        "location_type": loc_types[0] if loc_types else "",
        "has_geo_data": bool(w.get("has_geo_data")),
        **route_stats,
    }


async def fetch_all_workouts(headers: dict) -> list[dict]:
    """Fetch all discoverable workouts from lycan, with concurrency."""
    if _cache_fresh(WORKOUTS_CACHE):
        print("  Using cached library (< 7 days old). Use --refresh to re-download.")
        with open(WORKOUTS_CACHE) as f:
            return json.load(f)

    sem = asyncio.Semaphore(CONCURRENCY)
    all_workouts: list[dict] = []

    async with httpx.AsyncClient(timeout=20) as client:
        # First, get page 1 to estimate total pages
        first = await _fetch_page(client, headers, 1, sem)
        all_workouts.extend(_slim_workout(w) for w in first)
        print(f"  Page 1: {len(first)} workouts")

        if len(first) < PAGE_SIZE:
            # Only one page
            pass
        else:
            # Fetch remaining pages in batches
            page = 2
            while True:
                batch_pages = list(range(page, page + CONCURRENCY * 2))
                tasks = [_fetch_page(client, headers, p, sem) for p in batch_pages]
                results = await asyncio.gather(*tasks)

                empty_count = 0
                for p, result in zip(batch_pages, results):
                    if not result:
                        empty_count += 1
                        continue
                    all_workouts.extend(_slim_workout(w) for w in result)

                last_page = batch_pages[-1]
                print(
                    f"  Pages {page}-{last_page}: "
                    f"{len(all_workouts)} total workouts",
                    flush=True,
                )

                if empty_count > 0 or any(len(r) < PAGE_SIZE for r in results if r):
                    break

                page = last_page + 1

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(WORKOUTS_CACHE, "w") as f:
        json.dump(all_workouts, f)
    print(f"  Cached {len(all_workouts)} workouts to {WORKOUTS_CACHE}")
    return all_workouts


def build_trainer_summary(
    workouts: list[dict],
    trainers: dict[str, dict],
    type_filter: str | None = None,
) -> list[dict]:
    """Group workouts by trainer, with stats."""
    by_trainer: dict[str, list[dict]] = defaultdict(list)

    for w in workouts:
        if type_filter:
            match = type_filter in w["type"].lower()
            if not match:
                match = any(type_filter in c.lower() for c in w["categories"])
            if not match:
                match = any(type_filter in c.lower() for c in w["subcategories"])
            if not match:
                continue

        tid = w["trainer_id"]
        by_trainer[tid].append(w)

    results = []
    for tid, wlist in by_trainer.items():
        tinfo = trainers.get(tid, {})
        name = tinfo.get("name", "")
        if not name and tid:
            name = f"(trainer {tid[:8]}...)"
        elif not name:
            name = "(no trainer)"

        types = defaultdict(int)
        cats = set()
        subcats = set()
        equip = set()
        total_rating = 0
        rating_count = 0

        for w in wlist:
            types[w["type"]] += 1
            cats.update(w["categories"])
            subcats.update(w["subcategories"])
            equip.update(w["equipment_types"])
            if w["rating_avg"] > 0:
                total_rating += w["rating_avg"]
                rating_count += 1

        avg_rating = round(total_rating / rating_count, 1) if rating_count else 0

        results.append({
            "trainer_id": tid,
            "trainer_name": name,
            "workout_count": len(wlist),
            "types": dict(types),
            "avg_rating": avg_rating,
            "categories": sorted(cats),
            "subcategories": sorted(subcats),
            "equipment_types": sorted(equip),
        })

    results.sort(key=lambda x: (x["trainer_name"].lower(), -x["workout_count"]))
    return results


def main() -> int:
    type_filter = None
    if "--type" in sys.argv:
        idx = sys.argv.index("--type")
        if idx + 1 < len(sys.argv):
            type_filter = sys.argv[idx + 1].lower()

    refresh = "--refresh" in sys.argv

    if refresh:
        for p in (TRAINERS_CACHE, WORKOUTS_CACHE):
            if os.path.exists(p):
                os.remove(p)

    headers = get_auth_headers()

    print("Fetching trainers...", flush=True)
    trainers = fetch_all_trainers(headers)
    print(f"  {len(trainers)} trainers loaded\n")

    print("Fetching library workouts...", flush=True)
    workouts = asyncio.run(fetch_all_workouts(headers))
    print(f"  {len(workouts)} workouts loaded\n")

    print("Building trainer summary...", flush=True)
    summary = build_trainer_summary(workouts, trainers, type_filter)

    total_workouts = sum(s["workout_count"] for s in summary)
    print(f"\n{'=' * 110}")
    print(f"  iFit Library: {total_workouts} workouts across {len(summary)} trainers")
    if type_filter:
        print(f"  Filter: {type_filter}")
    print(f"{'=' * 110}")

    for s in summary:
        name = s["trainer_name"]
        count = s["workout_count"]
        rating = s["avg_rating"]
        type_str = ", ".join(f"{t}({n})" for t, n in sorted(s["types"].items(), key=lambda x: -x[1]))
        cats = ", ".join(s["subcategories"][:5]) or ", ".join(s["categories"][:3])

        print(
            f"\n  {name}  ({count} workouts, ★{rating:.1f})"
        )
        print(f"    Types: {type_str}")
        if cats:
            print(f"    Focus: {cats}")
        if s["equipment_types"]:
            print(f"    Equipment: {', '.join(s['equipment_types'][:5])}")

    # Save full data
    out_path = os.path.join(CACHE_DIR, "library_by_trainer.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n\nSaved to .ifit_capture/library_by_trainer.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
