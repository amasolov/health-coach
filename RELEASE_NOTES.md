# Release Notes

## v0.28.2
**OpenRouter credit exhaustion handling and admin notifications**

- Catch HTTP 402 (Payment Required) from OpenRouter and show users a clear message instead of a traceback
- Send a Home Assistant persistent notification to the admin when credits run out (uses Supervisor API)
- Handle HTTP 429 (rate limit) and 5xx (server errors) with appropriate user-facing messages
- All API errors logged to `ops_log` for operational visibility
- Admin notification is sent once per process lifetime to avoid spam

## v0.28.1
**Fix Hevy routine creation failures**

- Fixed broken R2 imports in `hevy_exercise_resolver.py` (`from r2_store` → `from scripts.r2_store`) — resolved exercises and custom exercise mappings were never being cached in R2, causing repeated LLM calls and duplicate custom exercise creation attempts
- Added JSON error handling in `create_hevy_routine` for cases where Hevy API returns 200/201 with an empty response body
- Added JSON error handling in `_create_custom_exercise` for empty response bodies
- Ensured exercise template IDs are always stored as strings (Hevy API may return integer IDs)
- Added diagnostic logging throughout the Hevy resolution and routine creation flow
- 4 new tests: empty API response handling, integer ID conversion, API error propagation

## v0.28.0
**Routine weight recommendations**

- New tool `get_routine_weight_recommendations` fetches a Hevy routine and recommends specific weights, reps, and sets for each exercise
- Analyses 90 days of exercise history to detect trends: progressing, plateau, declining, or new
- Progressive overload logic: weight bumps for compounds when reps hit threshold, rep increases for isolations, deload for easy days
- Fatigue-aware: factors in TSB, body battery, cardio leg stress with proportional adjustments
- Compound vs isolation detection for appropriate increment sizes (2.5kg vs 1kg)
- Lists available routines if none specified; supports name-based fuzzy matching
- 25 new tests covering history analysis, recommendation logic, and full tool flow

## v0.27.1
**Scope guardrails for chatbot**

- Chatbot system prompt now explicitly limits conversation to health, fitness, and training topics
- Off-topic requests are politely declined with a redirect
- Hardened against prompt injection to bypass boundaries
- MCP server instructions updated to reflect fitness-only scope

## v0.27.0
**Cardio-aware muscle fatigue in strength recommendations**

- Running, cycling, hiking, and climbing now inject "virtual volume" into the muscle load tracker
- New `CARDIO_MUSCLE_STRESS` mapping with per-activity-type stress factors (running: 1.0, cycling: 0.7, hiking: 0.5, etc.)
- `cardio_leg_stress` continuous score (0-100) replaces the simple boolean flag
- Recency decay: yesterday = full weight, 2 days ago = 60%, 3+ days = 30%
- Stage 1 goal alignment uses continuous stress for all users, not just runners
- Stage 2 exercise scoring applies proportional penalty/boost based on cardio intensity
- 9 new tests covering cardio-aware scoring paths

## v0.26.2
**Comprehensive test suite**

- 132 tests across 15 test files covering all MCP tool categories
- In-memory `FakeR2Store` for isolated R2 testing
- All external API calls (Hevy, GitHub, Garmin, OpenRouter) fully mocked
- MCP protocol connectivity tests (optional, for live addon)
- Fixed bug: `compare_hevy_workout` called `_get_conn()` instead of `get_conn()`

## v0.26.1
**iFit program week structure**

- Programs stored in R2 now include `workoutSections` (weekly structure)
- `get_ifit_program_details` returns structured week-by-week schedule with workout positions
- `backfill_program_weeks()` retroactively updates existing programs missing week data
- Users can now find "Week 1, Workout 1" of any series

## v0.26.0
**iFit-to-Hevy feedback loop**

- Persist iFit-to-Hevy routine mapping in R2 when routines are created
- `routine_id` column added to `strength_sets` table (migration 004)
- New tools: `get_hevy_routine_review`, `compare_hevy_workout`, `apply_exercise_feedback`
- Users can review conversions, compare with completed workouts, and apply corrections
- Exercise corrections clear the resolved exercise cache for re-resolution

## v0.25.0
**iFit-to-Hevy routine creation pipeline**

- Two-stage strength workout recommendation: metadata scoring + LLM exercise extraction
- `hevy_exercise_resolver` module: ID match → fuzzy name match → LLM classification → custom exercise creation
- Hevy exercise resolution and custom exercise map cached in R2
- On-demand series discovery via `discover_ifit_series` tool
- Series discovery from `pre-workout` API with full program mapping
- Numeric challenge IDs filtered from program discovery
- Search scoring improvements for phrase matching

## v0.24.0
**iFit series discovery**

- New `pre-workout/{workoutId}` API endpoint discovered via MITM
- Hybrid series discovery: immediate for synced workouts, incremental batched, on-demand for lookups
- Workout-to-series mapping stored in R2 (one-to-many)
- Periodic refresh to keep series data current

## v0.23.0 – v0.23.3
**R2 persistent storage and iFit improvements**

- Cloudflare R2 integration for persistent iFit data (transcripts, exercises, library, programs)
- iFit program search via R2-backed index
- Exercise correction reporting via GitHub issues
- Tool display names updated for Chainlit's "Used" prefix
- Detailed logging for iFit R2 sync pipeline
- On-demand exercise extraction for `get_ifit_workout_details`

## v0.22.0
**iFit library cache in production**

- Build-time iFit library cache for instant search
- Library includes workout descriptions for better search relevance

## v0.21.0
**Full iFit integration for chatbot**

- iFit workout search, recommendations, details, and treadmill workout generation
- Chatbot can search 12,000+ iFit workouts by name, trainer, or description

## v0.20.0
**Dashboard timezone fixes**

- All Grafana dashboards respect user timezone
- Consistent date handling across all SQL queries

## v0.16.0
**TSS calculation overhaul**

- Fixed TSS calculation for running and cycling
- Improved iFit recommendations
- Dashboard improvements

## v0.13.0 – v0.15.0
**Stability and data quality**

- Fixed Chainlit database metadata columns
- Auto-populate athlete thresholds from Garmin on first sync
- Improved TSS estimation with LTHR backfill

## v0.10.0 – v0.12.0
**Sync improvements**

- Full sync mode for backfilling historical data
- Garmin re-authentication flow
- PWA support and persistent chat history
- Logo rebrand

## v0.7.0 – v0.9.0
**Core features**

- Sync-on-demand from chatbot
- Feature suggestion tool (GitHub issues)
- Onboarding improvements
- PWA support

## v0.1.0 – v0.4.0
**Foundation**

- Initial health and fitness tracking framework
- Garmin Connect and Hevy data sync
- TimescaleDB schema with time-series tables
- PMC calculation (CTL/ATL/TSB)
- MCP server with multi-user authentication
- Chainlit chat UI with OpenRouter LLM
- Athlete profile auto-fetch from Garmin
- Data-driven fitness assessment for onboarding
- User goals, action items, and integrations registry
- Grafana dashboards (activities, vitals, body composition, strength, PMC)
- GitHub Actions CI/CD build pipeline
- Home Assistant add-on packaging
