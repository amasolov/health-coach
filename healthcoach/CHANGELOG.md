# Release Notes

## v0.53.0
**Migrate OAuth / credential storage from local files to DB (issue #29)**

- **`credentials` table** — new Alembic migration (`0003`) creates a `credentials` table with JSONB storage, per-user and system-wide unique indexes, and `ON DELETE CASCADE` for user cleanup
- **`credential_store` module** — `CredentialStore` ABC with `DBCredentialStore` (PostgreSQL) and `FileCredentialStore` (local JSON) implementations; module-level `get_credential` / `put_credential` / `delete_credential` / `get_credential_locked` API
- **Auto-detection** — the store probes DB connectivity on first use and caches the choice; falls back to file storage when the database is unreachable
- **Garmin OAuth** — `garmin_auth.py` now persists tokens via `client.garth.dumps()` (base64) into the credential store on login and MFA completion; `try_cached_login` checks DB before the local token directory
- **iFit OAuth** — `ifit_auth.py` reads/writes tokens through the credential store; `refresh_token` uses `SELECT … FOR UPDATE` row-level locking to prevent concurrent refresh races
- **Chainlit auth secret** — `addon_config.py` resolves the JWT secret DB-first, migrates an existing file-based secret into DB on first read, and generates a new secret into both DB and file when none exists
- **File backward compatibility** — all three credential types continue to work with local files for HA addon deployments or when DB is unavailable
- **24 new tests** in `test_credential_store.py` covering DB store, file store, auto-detection, locked reads, and integration with Garmin auth, iFit auth, and Chainlit secret

## v0.52.0
**Fix timezone bugs causing wrong-day vitals/sleep/recovery data**

- **UTC-aware Garmin parsing** — `_parse_garmin_datetime` and `_extract_vitals` now return UTC-aware datetimes instead of naive; eliminates server-timezone-dependent storage
- **Timezone-aware SQL boundaries** — `get_vitals`, `get_activities`, `get_training_load`, `get_body_composition`, and `get_strength_sessions` use `datetime.combine(..., tzinfo=tz)` so date ranges respect the user's timezone
- **Eliminated bare `date.today()` / `datetime.now()`** — replaced with `user_today(tz)` / `utc_now()` in `health_tools.py`, `garmin_fetch.py`, `athlete_store.py`, `ifit_strength_recommend.py`
- **Explicit tz propagation** — `_db_history_entries`, `generate_action_items`, `fetch_recent_history` now accept and use a `tz` parameter instead of silently falling back to `DEFAULT_TZ`
- **sync_vitals date-range fix** — converts UTC `last` to user timezone before `.date()` to prevent off-by-one at day boundary
- **Cursor rule rewrite** — `.cursor/rules/timezone-handling.mdc` is now `alwaysApply: true` with 10 sections: golden rules, banned patterns, SQL boundaries, sleep/vitals specifics, and code review checklist
- **11 new tests** in `test_timezone.py` including AST-based static analysis that scans all scripts for bare `date.today()`, `datetime.now()`, and `user_today()`/`user_now()` without explicit tz

## v0.51.1
**Don't recommend iFit-sourced Hevy routines (issue #25)**

- **iFit-source annotation** — `list_hevy_routines` now cross-references the R2 routine map and adds an `ifit_source` field (with original workout ID and title) to routines that were auto-created from iFit workouts
- **Separated listing** — `manage_hevy_routines` (action=list) splits routines into user-created vs iFit-sourced groups; LLM instructions tell the model not to recommend iFit-sourced routines as workouts
- **Weight recommendations filtering** — `get_routine_weight_recommendations` excludes iFit-sourced routines when listing available routines (direct ID lookup still works)
- **System prompt guidance** — both Chainlit and Telegram system prompts now instruct the LLM to never recommend iFit-sourced Hevy routines and to suggest the original iFit workout instead
- **4 new tests** covering annotation, separation, filtering, and the no-iFit-routines case

## v0.51.0
**Telegram-compatible rich text formatting (issue #19)**

- **Markdown → Telegram HTML conversion** — new `scripts/telegram_format.py` module converts standard LLM Markdown (`**bold**`, `*italic*`, `` `code` ``, fenced code blocks, links, headings, lists, blockquotes, strikethrough) into the HTML subset Telegram accepts (`<b>`, `<i>`, `<code>`, `<pre>`, `<a>`, `<blockquote>`, `<s>`)
- **HTML parse mode** — bot responses are now sent with `parse_mode="HTML"` so Telegram renders bold, italic, code, and other formatting natively instead of showing raw markdown characters
- **Graceful fallback** — if Telegram rejects a chunk (malformed HTML), the bot resends it as plain text so users always get the message
- **BadRequest not retried** — `_send_with_retry` now excludes `BadRequest` from its retry loop since format errors won't self-heal
- **59 new tests** — `test_telegram_format.py` (48 tests covering all formatting constructs, escaping, emoji, realistic LLM output) and `test_telegram_bot.py` (11 tests for HTML sending, fallback, chunking)

## v0.50.0
**Pydantic BaseSettings for unified configuration (issue #9)**

- **Single config singleton** — `addon_config.config` is now the canonical entry point for all configuration; modules read typed attributes (`config.openrouter_api_key`, `config.grafana_host`, …) instead of raw `os.environ.get()` calls
- **Automatic `.env` loading** — importing `addon_config` calls `load_dotenv()` once at module level; individual scripts no longer need their own `load_dotenv()` calls
- **New fields** — `embedding_dim`, `mcp_host`, `mcp_api_key`, `github_repo`, `supervisor_token`, `hevy_api_key`, `grafana_ingress_path`, `grafana_db_host`, `sync_max_retries`, `sync_retry_base`, `sync_user_timeout`
- **DB connection consolidation** — `db_pool.dsn_kwargs()` (now public) replaces six duplicated 5-line `os.environ` blocks in `calc_pmc`, `sync_garmin`, `sync_hevy`, `run_sync`, `run_migrate`, and `setup_chainlit_db`
- **17 modules converted** — `r2_store`, `knowledge_store`, `task_runner`, `mcp_server`, `chat_app`, `telegram_bot`, `push_dashboards`, `health_tools`, `hevy_exercise_resolver`, `ifit_strength_recommend`, `ifit_llm_extract`, `ingest_books`, and the six sync scripts above
- **Test compatibility** — DB settings remain env-var-driven (via `dsn_kwargs()`) so the testcontainer fixture continues to work; tests that need to override non-DB config use `patch.object` on the config singleton
- **`pydantic-settings`** added to project dependencies

## v0.49.0
**Isolated database testing with testcontainers (issue #10)**

- **Ephemeral TimescaleDB container** — every `pytest` run now spins up a fresh `timescale/timescaledb-ha:pg17` container via testcontainers-python; Alembic migrations run automatically against it and synthetic seed data is inserted before tests execute
- **Zero production risk** — tests no longer connect to the shared production database; the `db_pool` connection pool is redirected to the container via env var overrides
- **Podman support** — auto-discovers the Podman machine socket on macOS and configures `DOCKER_HOST` / disables Ryuk; falls back to Docker transparently
- **Synthetic seed data** — new `tests/seed_data.py` provides a test user, 15 activities, 30 days of vitals/body composition, 120 days of training load (with projections), strength sets with routine IDs, athlete config, and threshold history — all date-relative so assertions never go stale
- **`integration` marker** — future tests that deliberately need the real production DB can use `@pytest.mark.integration`; excluded from the default test run alongside `slow`

## v0.48.0
**Threshold history for date-aware TSS calculation**

- **New `threshold_history` table** — stores dated snapshots of key thresholds (FTP, LTHR, resting HR, max HR, weight) via Alembic migration `0002`; indexed for fast `effective_date <= activity_date` lookups
- **Automatic snapshot recording** — every Garmin threshold refresh and manual `update_athlete_profile` call now upserts the current flat thresholds into the history table with today's date
- **Date-aware TSS estimation** — `sync_garmin`, `backfill_strength_tss`, and `backfill_missing_tss` now look up the thresholds that were effective at each activity's date instead of always using the latest values; prevents LTHR/FTP changes from distorting historical TSS
- **Timeline batch lookup** — `load_threshold_timeline()` + `pick_thresholds()` in `athlete_store` load all history rows in a single query for efficient per-activity resolution during backfill passes
- **Backward compatible** — activities processed before this version keep their existing TSS values; the history lookup falls back to current `athlete_config` thresholds when no history row exists yet

## v0.47.0
**Alembic migration framework (issue #16)**

- **Alembic replaces hand-rolled migration runner** — `run_migrate.py` now drives migrations via Alembic's `command.upgrade("head")` instead of manually iterating `.sql` files and tracking them in a `_migrations` table
- **Baseline migration** — all 11 existing SQL migrations consolidated into a single idempotent Alembic revision (`0001_baseline`); uses `IF NOT EXISTS` guards throughout so it's safe to run on both fresh and existing databases
- **Automatic transition** — on first run, `run_migrate.py` detects the legacy `_migrations` table and stamps the baseline revision without re-executing SQL; subsequent runs use Alembic natively
- **Down migrations** — future revisions can include `downgrade()` for rollback; the baseline itself raises an error (restore from backup instead)
- **Raw SQL workflow** — no ORM models required; new migrations use `op.execute()` with plain SQL, matching the existing codebase style
- **Taskfile targets** — `db:revision`, `db:downgrade`, `db:current`, `db:history` for creating and managing migrations from the CLI
- **Old `db/migrations/` preserved** — the original `.sql` files remain as a reference; they are no longer executed at runtime

## v0.46.0
**Automatic zone recalculation and dev tooling cleanup**

- **Auto zone recalc** — training zone boundaries (HR, power, pace) are now recalculated automatically during each sync cycle after Garmin thresholds are refreshed; new `recalculate_zones(slug)` function in `calc_zones.py` replaces the manual `task zones:calculate` workflow
- **Slimmer Docker image** — 13 dev-only scripts (MITM captures, interactive CLI tools, build-time generators) excluded from the production image via `.dockerignore`
- **Taskfile cleanup** — removed `mcp:serve`, `chat:serve`, and `ollama:*` targets that duplicated addon functionality; `addon:build` now uses the HA base image instead of `python:3.12-alpine`
- **Dead file removal** — deleted `config/athlete.example.yaml` (athlete config lives in DB since v0.32.0); simplified `.env.example` to DB + API keys only

## v0.45.0
**s6-overlay process supervisor — replaces run.sh (issue #7)**

- **s6-overlay as PID 1** — the 327-line `run.sh` bash entrypoint is replaced by s6-overlay's process supervisor; all four long-running services (MCP, Chainlit, Telegram, sync) are now individually supervised with automatic restart on crash and 5-second backoff
- **`rootfs/etc/services.d/`** — each service has its own `run` and `finish` script; conditional services (Chainlit, Telegram) sleep indefinitely when their required env vars are absent, keeping s6 happy without consuming resources
- **`rootfs/etc/cont-init.d/init.sh`** — single init script delegates to `scripts/init_addon.py`, which performs all one-shot setup: config export, file linking, DB migrations, user migration, Garmin auth warm-up, knowledge-base indexing, and Grafana provisioning
- **Pydantic Settings config** — `scripts/addon_config.py` provides a typed `AddonConfig` model loaded from `/data/options.json` (HA runtime) or environment variables (local dev); eliminates the fragile `eval "$(python3 -c ...)"` config hack
- **s6 container environment** — `write_s6_env()` exports all settings as individual files under `/run/s6/container_environment/` so that `with-contenv` bash scripts inherit them
- **CI aligned to HA base images** — GitHub Actions workflow now uses arch-specific `ghcr.io/home-assistant/*-base-python:3.12-alpine3.21` images (matching `build.yaml`) instead of `python:3.12-alpine`, ensuring CI and production use the same s6-overlay-enabled base
- **Separated log streams** — each service's stdout/stderr is managed independently by s6, enabling per-service log filtering
- **Dependency ordering** — init scripts complete before any service starts, guaranteeing migrations run before MCP/Chainlit/Telegram come up

## v0.44.0
**Async task runner with per-user parallelism and retry (issue #14)**

- **APScheduler task runner** — replaces the bash `while true; sleep N; done` loop with a Python-native `AsyncIOScheduler`; sync cycles are triggered on the configured interval with missed-fire coalescing
- **Per-user parallel sync** — users are synced concurrently via `asyncio.gather`; each user's 6-step sync pipeline (Garmin, Hevy, templates, thresholds, strength TSS, TSS backfill) still runs sequentially within that user
- **Retry with exponential back-off** — transient failures (API rate limits, timeouts) are retried up to `SYNC_MAX_RETRIES` times (default 2) with exponential delay (10s, 20s, 40s)
- **Per-user timeout** — each user's sync is capped at `SYNC_USER_TIMEOUT` seconds (default 600) so a hung API call cannot stall the entire cycle
- **Structured ops_log events** — every cycle emits `sync_cycle` with `parallel=True`, user count, error count, and timeout count
- **`run_sync.py` refactored** — extracted `sync_one_user(user)` and `sync_global()` from `main()` for reuse by the task runner; `main()` still works standalone for local dev / CLI
- **Graceful shutdown** — task runner handles SIGTERM/SIGINT; `run.sh` cleanup trap includes the new process

## v0.43.0
**Async MCP tools — non-blocking event loop (issue #11)**

- **All 40 MCP tools are now `async def`** — each tool dispatches its sync `health_tools` call to a worker thread via `anyio.to_thread.run_sync`, preventing psycopg2 queries from blocking the Starlette/FastMCP event loop
- **Unified `_wrap` helper** — the existing error-handling wrapper now also handles thread dispatch; tools that previously called `health_tools.*` directly now go through `_wrap` for consistent threading and `ValueError` → `ToolError` conversion
- **Auth middleware threaded** — `resolve_user_id` in `BearerAuthMiddleware` is also dispatched to a worker thread
- **`telegram_link.py` bug fix** — added missing `import psycopg2` that would have caused a `NameError` on `IntegrityError` during Telegram account linking conflicts

## v0.42.0
**Migrate users.json to the database**

- **User credentials in DB** — all user metadata (email, names, Garmin/Hevy credentials, MCP API keys, onboarding status) now lives in the `users` table instead of a JSON file on disk; new columns added via migration `011_users_credentials.sql`
- **Shared `load_all_users()`** — single DB query replaces 4 separate `USERS_JSON` env-var parsers across `chat_app`, `telegram_bot`, `mcp_server`, and `run_sync`; falls back to `USERS_JSON` env var when DB is unreachable (local dev)
- **`register_user()` writes to DB** — new user registration inserts all fields into the `users` table in a single INSERT instead of writing to `users.json`
- **Credential updates via DB** — `_persist_garmin_creds` and `_persist_hevy_key` now UPDATE the `users` row directly
- **One-time data migration** — `run.sh` reads `users.json` on first startup, upserts all entries into the DB, then renames the file to `users.json.migrated`
- **`USERS_JSON` env var eliminated** — no longer exported by `run.sh`; Garmin warm-up and user listing now query the DB directly

## v0.41.0
**Mandatory onboarding completion**

- **`onboarding_complete` gate** — a new flag in `users.json` is set only after the full onboarding flow finishes (sync, profile fetch, summary); if a user disconnects mid-onboarding and logs back in, their partial registration is automatically cleaned up and onboarding restarts from scratch
- **Automatic teardown** — both OAuth and password auth callbacks detect incomplete users and remove the partial DB record, `users.json` entry, and athlete config before restarting the registration flow
- **Backfill on startup** — `run.sh` sets `onboarding_complete: true` for all existing users so they are not affected by the new gate
- **`delete_user` / `athlete_store.delete`** — new cleanup functions for removing partially-created users across all stores

## v0.40.0
**Self-service Hevy Connect via chat**

- **`hevy_connect` tool** — users can connect or reconnect their Hevy account by providing their API key through the chatbot; the key is validated against the Hevy API before being persisted
- **`hevy_auth_status` tool** — check whether a Hevy API key is configured and valid, available in both Chainlit and Telegram
- **Credential persistence** — on successful validation the API key is saved to `users.json` and updated in the in-memory registry, same pattern as Garmin credentials

## v0.39.0
**Self-service Garmin Connect via chat**

- **Chat-based Garmin login** — users can now connect (or reconnect) their Garmin account directly through the chatbot by providing their email and password; the `garmin_authenticate` tool accepts optional `garmin_email` / `garmin_password` parameters and prompts the LLM to ask the user if credentials are missing
- **Credential persistence** — on successful authentication (or MFA initiation), credentials are saved to `users.json` and updated in the in-memory registry so subsequent syncs work without manual config editing
- **Telegram Garmin tools enabled** — `garmin_authenticate`, `garmin_submit_mfa`, and `garmin_auth_status` are no longer excluded from the Telegram bot, allowing users to manage their Garmin connection from any channel

## v0.38.0
**Accurate body battery and vitals refresh**

- **Intraday body battery** — vitals sync now fetches the Garmin body battery timeline for the current day, providing accurate high/low/latest values instead of stale daily summary data; new `body_battery_latest` column stores the most recent reading
- **True vitals upsert** — today's vitals row is always refreshed on each sync cycle (previously skipped once inserted), so body battery, stress, and other metrics stay current throughout the day
- **Current date in LLM system prompt** — both Chainlit and Telegram system prompts now include the user's local date/time and timezone, eliminating "today" vs "yesterday" misclassification

## v0.37.0
**Timezone-aware timestamps and iFit import fix**

- **Timezone-aware output** — tool dispatchers in both Chainlit and Telegram now auto-inject the user's configured timezone (`tz_name`) into all uid-based tool calls; `get_activities`, `get_strength_sessions`, `get_training_load`, `get_body_composition`, `get_vitals`, and `get_workout_summary` all convert timestamps to the user's local time before returning, eliminating "today"/"yesterday" misinterpretation by the LLM
- **Fixed `ifit_auth` import** — bare `from ifit_auth import ...` at module level in `ifit_strength_recommend.py`, `ifit_recommend.py`, and `ifit_list_series.py` now uses `scripts.ifit_auth` with fallback, fixing `"No module named 'ifit_auth'"` errors in the chatbot
- **Fixed `health_tools` import** — `gather_athlete_state` now uses `scripts.health_tools` qualified import

## v0.36.0
**Cross-platform workout deduplication (Garmin + Hevy)**

- **Smarter matching** — replaced naive same-calendar-day matching with time-window overlap (30-min buffer); correctly handles multiple strength sessions in one day by picking the closest Garmin activity by start time
- **Enriched sync results** — `sync_data` now runs `backfill_strength_tss` after syncing both platforms, and returns `cross_platform` section showing matched Garmin/Hevy workout pairs and Hevy-only workouts; sync period (`from`/`to` dates) included in Garmin results
- **New `get_workout_summary` tool** — unified view that merges Garmin metrics (HR, duration, calories) with Hevy exercise details (sets, reps, weight, volume) for strength workouts; a single physical session tracked on both platforms appears as one record with `sources: ["garmin", "hevy"]`

## v0.35.0
**Connection pooling, shared HTTP clients, and async event loop improvements**

- **DB connection pool** — replaced per-query `psycopg2.connect()` with a `ThreadedConnectionPool` in new `db_pool.py` module; all hot-path files (`health_tools`, `cross_channel`, `telegram_link`, `ops_emit`, `user_manager`, `athlete_store`, `knowledge_store`) now borrow/return connections from the pool, saving ~200-500ms per message
- **Shared httpx clients** — new `http_clients.py` module provides persistent `httpx.Client` instances for Hevy, iFit, and OpenRouter APIs; TCP connections and TLS sessions are reused across requests, eliminating ~200-400ms of handshake overhead per call
- **Async event loop unblocking** — wrapped all blocking DB and rendering calls in `telegram_bot.py` with `asyncio.to_thread()` (`get_user_by_telegram`, `_get_messages`, `save_telegram_message`, `validate_link_code`, `clear_telegram_history`, `maybe_chart`, `_render_chart_png`); fire-and-forget calls (`ops_emit.emit`, assistant message save) use `run_in_executor` to avoid blocking the event loop for concurrent users

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
- **Athlete config moved to PostgreSQL** — new `athlete_config` table (JSONB) replaces `athlete.yaml` as the source of truth; `athlete_store.py` module provides load/save/update; the legacy YAML file has been removed
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
