# Health Coach

AI-powered personal health and fitness coaching platform, deployed as a [Home Assistant](https://www.home-assistant.io/) add-on.

Syncs data from **Garmin Connect** and **Hevy**, calculates performance metrics (CTL / ATL / TSB), integrates the full **iFit** workout library, provisions **Grafana** dashboards, and provides an AI chat interface for data-driven coaching.

## Features

### Data Integration
- **Garmin Connect** — activities, vitals (HRV, sleep, stress, body battery), body composition
- **Hevy** — strength training sets, reps, weights, exercise templates
- **iFit** — 12,000+ workout library with transcript-based exercise extraction via LLM

### Analytics
- **Performance Management Chart (PMC)** — CTL (fitness), ATL (fatigue), TSB (form), ramp rate with 8-week projections
- **Training Stress Score (TSS)** — calculated for running (pace/HR), cycling (power), and strength (volume-based)
- **Cardio-aware muscle fatigue** — running/cycling stress is tracked as lower-body load when recommending strength workouts

### AI Coaching (Chainlit Chat UI)
- Conversational fitness coach backed by OpenRouter LLMs
- 40+ specialized tools for querying training data, recommending workouts, and managing goals
- **RAG knowledge base** — upload fitness PDFs (books, guides, research) and the coach references them in recommendations
- Scope-limited to health and fitness topics only
- iFit workout recommendations with two-stage scoring (metadata + LLM exercise analysis)
- iFit-to-Hevy routine conversion with exercise matching and custom exercise creation
- Feedback loop: compare completed Hevy workouts against iFit predictions and apply corrections

### MCP Server
- [Model Context Protocol](https://modelcontextprotocol.io/) endpoint (Streamable HTTP) for external AI clients
- Bearer token authentication with multi-user support
- All coaching tools available programmatically

### Dashboards (Grafana)
- Home overview, Activities, PMC, Vitals, Body Composition, Strength, Ops

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Home Assistant Add-on (Docker)                     │
│                                                     │
│  ┌──────────┐  ┌────────────┐  ┌────────────────┐  │
│  │ Chainlit │  │ MCP Server │  │  Sync Daemon   │  │
│  │ Chat UI  │  │ (FastMCP)  │  │  (cron loop)   │  │
│  │ :8080    │  │ :8765      │  │                │  │
│  └────┬─────┘  └─────┬──────┘  └───────┬────────┘  │
│       │              │                  │           │
│       └──────────┬───┘                  │           │
│                  ▼                      ▼           │
│         ┌──────────────┐      ┌──────────────┐     │
│         │ health_tools │      │  sync_garmin │     │
│         │  (40+ tools) │      │  sync_hevy   │     │
│         └──────┬───────┘      │  calc_pmc    │     │
│                │              └──────┬───────┘     │
│                ▼                     ▼             │
│  ┌──────────────────────────────────────────────┐  │
│  │            TimescaleDB (PostgreSQL)           │  │
│  │  activities · vitals · body_comp · strength   │  │
│  │  training_load · users · ops_log              │  │
│  │  documents · knowledge_chunks (pgvector)      │  │
│  └──────────────────────────────────────────────┘  │
│                │                                    │
│                ▼                                    │
│  ┌───────────────┐  ┌─────────────────────────┐   │
│  │    Grafana     │  │   Cloudflare R2 (S3)   │   │
│  │  (dashboards)  │  │  transcripts, exercises │   │
│  └───────────────┘  │  programs, series map   │   │
│                      └─────────────────────────┘   │
└─────────────────────────────────────────────────────┘

External APIs: Garmin Connect · Hevy · iFit · OpenRouter · GitHub
```

## Prerequisites

| Component | Purpose |
|-----------|---------|
| Home Assistant OS | Host platform |
| TimescaleDB add-on | Time-series database |
| Grafana add-on | Dashboard visualization |
| Garmin Connect account | Activity and health data |
| OpenRouter API key | LLM for chat and exercise extraction |

Optional:
- **Hevy** account + API key — strength training tracking
- **iFit** account — workout library access
- **Cloudflare R2** bucket — persistent iFit data cache
- **GitHub** token — feature suggestion and exercise correction issues

## Installation

### As a Home Assistant Add-on

1. Add this repository to your Home Assistant add-on store
2. Install the **Health Coach** add-on
3. Configure the add-on options (database, API keys)
4. Start the add-on

### Local Development

```bash
git clone https://github.com/amasolov/health-coach.git
cd health-coach

# Install dependencies
pip install -e ".[dev]"

# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your DB credentials, API keys, etc.

# Run database migrations
python -m scripts.run_migrate

# Start the sync daemon
python -m scripts.run_sync

# Start the chat UI
chainlit run scripts/chat_app.py -p 8080

# Start the MCP server
python -m scripts.mcp_server
```

## Configuration

All configuration is managed through the Home Assistant add-on options panel. Key settings:

| Option | Default | Description |
|--------|---------|-------------|
| `sync_interval_minutes` | 30 | How often to sync external data |
| `chat_model` | `anthropic/claude-sonnet-4` | OpenRouter model for chat |
| `mcp_port` | 8765 | MCP server port |
| `chat_port` | 8080 | Chainlit UI port |
| `allow_registration` | false | Enable self-service user onboarding |

Secrets (API keys, DB credentials) are configured in the same panel and stored securely by Home Assistant.

## Testing

```bash
# Run the full test suite (requires DB access via .env)
python -m pytest tests/

# Skip slow tests (iFit library search)
python -m pytest tests/ -m "not slow"

# Run specific test category
python -m pytest tests/test_fitness_pmc.py -v

# Run MCP connectivity tests (requires running addon)
MCP_TEST_URL=http://your-addon:8765/mcp MCP_TEST_TOKEN=your-token \
  python -m pytest tests/test_mcp_protocol.py -v
```

The test suite mocks all external API calls (Hevy, iFit, GitHub, OpenRouter, Garmin) so no real data is ever created in third-party services.

## Project Structure

```
├── healthcoach/
│   ├── config.yaml          # HA add-on manifest
│   ├── Dockerfile           # Container build
│   └── run.sh               # Entrypoint (migrations, sync, servers)
├── scripts/
│   ├── chat_app.py          # Chainlit chat UI
│   ├── mcp_server.py        # FastMCP server
│   ├── health_tools.py      # 40+ coaching tool implementations
│   ├── chat_tools_schema.py # OpenAI function-calling schemas
│   ├── knowledge_store.py   # RAG: PDF ingestion, embedding, retrieval
│   ├── run_sync.py          # Sync orchestrator
│   ├── sync_garmin.py       # Garmin Connect data sync
│   ├── sync_hevy.py         # Hevy data sync
│   ├── calc_pmc.py          # PMC / TSS calculation
│   ├── ifit_*.py            # iFit integration modules
│   ├── hevy_exercise_resolver.py  # iFit→Hevy exercise matching
│   └── ...
├── db/migrations/           # SQL migrations (auto-applied)
├── grafana/dashboards/      # Dashboard JSON (auto-provisioned)
├── config/athlete.yaml      # Per-user athlete profiles
├── tests/                   # pytest suite (132 tests)
└── .github/workflows/       # CI/CD build pipeline
```

## Versioning

This project uses semantic versioning (`x.y.z`):
- **x** — major breaking changes
- **y** — new features
- **z** — bug fixes and minor improvements

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
