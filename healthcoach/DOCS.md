# Health Coach Addon

AI-powered personal health and fitness coach. Syncs data from Garmin Connect
and Hevy into TimescaleDB, calculates performance metrics (CTL/ATL/TSB),
provisions Grafana dashboards, and serves an MCP endpoint and Chainlit chat UI
for AI-powered coaching.

## Setup

1. Install the TimescaleDB and Grafana addons on your Home Assistant instance
2. Create a database called `health` in TimescaleDB
3. Configure this addon with your database credentials and API keys
4. (Optional) Generate a Grafana service account API key for dashboard provisioning

## Multi-User

Add additional users in the addon configuration, or enable `allow_registration`
to let users self-register through the chat UI. Each user needs their own
Garmin Connect and/or Hevy credentials. Data is isolated per user in the
database and filterable via the Grafana user selector dropdown.

## MCP Server

The addon runs a Model Context Protocol (MCP) server on port 8765
(configurable). Each user gets a unique API key (auto-generated if left
blank). Connect any MCP-compatible AI tool for personalized training insights.

### Available Tools (27)

**Data & Metrics:**
- **get_fitness_summary** -- current CTL/ATL/TSB, ramp rate, form status
- **get_training_load** -- daily PMC data for a date range
- **get_activities** -- list activities filtered by date/sport
- **get_activity_detail** -- full metrics for a single activity
- **get_body_composition** -- weight, body fat, muscle mass trends
- **get_vitals** -- resting HR, HRV, blood pressure, sleep, stress
- **get_training_zones** -- HR, power, and pace zones
- **get_athlete_profile** -- thresholds and training status
- **get_strength_sessions** -- Hevy strength training data

**Workouts:**
- **list_treadmill_templates** -- available workout templates
- **generate_treadmill_workout** -- create a structured treadmill workout

**Garmin Auth:**
- **garmin_auth_status** -- check Garmin Connect auth state
- **garmin_authenticate** -- start Garmin login flow
- **garmin_submit_mfa** -- complete MFA verification

**Profile & Setup:**
- **garmin_fetch_profile** -- pull profile data from Garmin Connect
- **generate_fitness_assessment** -- comprehensive data-driven fitness overview
- **update_athlete_profile** -- update a single profile field

**Goals & Onboarding:**
- **get_onboarding_questions** -- questions for new user goal setting
- **set_user_goals** / **get_user_goals** -- store and retrieve goals

**Action Items:**
- **get_action_items** -- review outstanding tasks
- **add_action_item** -- create a new task
- **update_action_item** -- update task status/details
- **complete_action_item** -- mark a task done

**Integrations & Hardware:**
- **get_supported_integrations** -- list all supported equipment and software
- **set_user_integrations** -- store user's hardware/software selections
- **get_user_integrations** -- get user's configured integrations

### Client Configuration

**Open WebUI** (Admin Settings → External Tools):
- URL: `http://healthcoach:8765/mcp`
- Type: MCP (Streamable HTTP)
- Auth: Bearer `<mcp_api_key>`

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "healthcoach": {
      "type": "http",
      "url": "http://<ha-ip>:8765/mcp",
      "headers": { "Authorization": "Bearer <mcp_api_key>" }
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "healthcoach": {
      "url": "http://<ha-ip>:8765/mcp",
      "headers": { "Authorization": "Bearer <mcp_api_key>" }
    }
  }
}
```

Your MCP API key is printed in the addon log on startup. If you left the
`mcp_api_key` field blank, a key is auto-generated and persisted.
