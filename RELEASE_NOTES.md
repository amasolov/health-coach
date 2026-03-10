# Release Notes

## v0.34.0
**Performance tracing and bottleneck fixes for iFit/Hevy operations**

- Added `perf` logger with wall-clock timing for key operations: `search_ifit_library`, `get_ifit_workout_details`, `create_hevy_routine_from_recommendation`, `_find_existing_routine`, and `resolve_hevy_exercises`
- **Parallelized** trainer fetch and exercise extraction in `get_ifit_workout_details` using `ThreadPoolExecutor` — these two independent HTTP chains now run concurrently instead of sequentially
- **Eliminated serial HTTP calls** in `search_ifit_library` — removed per-result `fetch_workout_series` enrichment that made up to 10 sequential HTTP requests for program metadata
- **Cached program index** in memory (`_program_index_cache`) so `load_program_index` R2 downloads happen once per process, not per search/detail call
- **Cached Hevy routine map** in memory (`_routine_map_cache`) to avoid repeated R2 downloads during duplicate checks
- Set up `logging.basicConfig` in Chainlit entrypoint for consistent log output

## v0.33.0
**Hevy routine management — list, rename, and duplicate cleanup**

- New `manage_hevy_routines` chatbot tool with three actions:
  - `list` — show all routines in the user's Hevy account
  - `rename` — rename a routine by ID (uses `PUT /v1/routines`)
  - `mark_duplicates` — find routines with identical titles and prefix extras with `[DELETE] ` for easy manual removal
- The public Hevy API does not support routine deletion; the mark-and-rename approach lets users spot duplicates in the app
- Added `hevy_mitm_capture.py` mitmproxy addon for capturing Hevy app traffic (used to reverse-engineer internal API endpoints)
- Tool available in both Chainlit web chat and Telegram bot

## v0.32.2
**Resilient Hevy routine creation with title search and stale-cache retry**

- Title-based fallback: when `ifit_workout_id` is missing or 404s, the tool searches the iFit library by `workout_title`
- Auto-retry on stale exercise IDs: if Hevy rejects a routine with "invalid exercise template id", the resolution cache is cleared and exercises are re-resolved from scratch
- LLM instructed to never guess workout IDs — always pass `workout_title` as fallback

## v0.32.1
**Fix Hevy routine creating the wrong workout from stale cache**

- Routine creation now identifies workouts by `ifit_workout_id` instead of a fragile positional index into a shared cache file
- If the workout isn't in the cached recommendations, exercises are fetched on-the-fly from the iFit API
- Tool schema and system prompt updated to guide the LLM to always pass the workout ID

## v0.32.0
**Hevy routine reliability, athlete config in DB, Garmin threshold tracking, Renovate**

- **Hevy custom exercise creation fixed** — the Hevy API returns a raw UUID (not JSON); the resolver now correctly parses this, eliminating "empty response body" failures
- **Pre-creation dedup check** — before creating a custom exercise, the resolver queries Hevy to avoid duplicates
- **Duplicate routine prevention** — `create_hevy_routine` checks the R2 mapping and Hevy routine list before creating; returns `already_exists` if found
- **Incomplete routine reporting** — when exercises fail to resolve, status is `created_incomplete` with a warning naming the missing exercises
- **System prompt for Hevy** — chatbot no longer shows "Manual Hevy Setup" instructions when routines are created automatically
- **Athlete config moved to PostgreSQL** — new `athlete_config` table (JSONB) replaces `athlete.yaml` as the source of truth; `athlete_store.py` module provides load/save/update with YAML fallback and dual-write during transition
- **DB migration 009** — `athlete_config` table with slug primary key and JSONB config column
- **Garmin threshold auto-sync** — `refresh_garmin_thresholds` fetches Garmin profile, compares with source-priority (lab values never overwritten), auto-updates non-lab fields, logs advisories
- **Running HR zones tool** — `setup_running_hr_zones` selects best estimation method from available data and provides Garmin watch configuration instructions
- **Pinned dependencies** — `requirements.txt` now has exact versions for reproducible builds
- **Renovate configured** — weekly dependency update PRs with automerge for patches, grouped minors, Docker digest pinning, GitHub Actions SHA pinning
- **Exercise type enum fix** — corrected `HEVY_EXERCISE_TYPES` to match official Hevy API (`bodyweight_assisted_reps`, `short_distance_weight`)
- **Tool display names** — added missing display names for `setup_running_hr_zones`, `search_knowledge_base`, `list_knowledge_documents`, `delete_knowledge_document`

## v0.31.0
**Cross-channel conversation context between Telegram and web chat**

- Telegram messages are now persisted to the database (survive bot restarts)
- When opening the web chat, the system prompt includes a summary of recent Telegram conversations (last 24h)
- When messaging via Telegram, the system prompt includes a summary of recent web chat conversations (last 24h)
- The LLM maintains coaching context across both channels — no need to repeat yourself
- New migration `008_telegram_messages.sql` for persistent Telegram history
- `/reset` command now clears both in-memory session and database history
- New shared module `scripts/cross_channel.py` for cross-channel context retrieval and formatting

## v0.30.0
**Telegram bot + configurable RAG embedding backend**

- **Telegram bot**: registered users can chat with the coach via Telegram after linking their account with a one-time code from the web UI
- Multi-layer credential sanitization prevents API tokens from leaking through Telegram
- Sensitive tools (Garmin auth, onboarding) excluded from the Telegram channel
- New tools: `generate_telegram_link_code`; commands: `/start`, `/unlink`, `/reset`
- New config options: `telegram_bot_token`, `telegram_bot_username`
- **RAG embedding backend is now configurable** — supports both OpenAI API (`text-embedding-3-small`) and local Ollama (`nomic-embed-text`); fastembed/onnxruntime removed (no musl wheels for Alpine)
- New config: `openai_api_key`, `embedding_api_base`, `embedding_model`
- Local dev workflow: `make ollama-setup` pulls the model, `make ingest-books` processes PDFs via local Ollama
- New CLI script `scripts/ingest_books.py` for batch PDF ingestion with progress reporting
- Standardised on 768-dim vectors (matches Ollama nomic-embed-text natively; OpenAI truncates via Matryoshka)
- Batched embedding calls (512 texts per request) for efficient large-PDF ingestion

## v0.29.0
**RAG knowledge base for fitness books and documents**

- Upload fitness PDFs (books, guides, research) and the coach will reference them when making recommendations
- Two upload paths: drop PDFs in `/config/healthcoach/knowledge/` (global, indexed on startup) or upload via chat (per-user)
- Vector storage and retrieval via pgvector on the existing TimescaleDB instance
- Three new tools: `search_knowledge_base`, `list_knowledge_documents`, `delete_knowledge_document`
- System prompt dynamically includes knowledge base availability when documents are present
- SHA-256 deduplication prevents re-indexing the same file
- New migration `007_knowledge_base.sql` adds pgvector extension, `documents` and `knowledge_chunks` tables

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
