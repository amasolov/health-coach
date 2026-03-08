#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"

if [[ ! -f "$OPTIONS_FILE" ]]; then
    echo "ERROR: $OPTIONS_FILE not found -- are we running inside HA?"
    exit 1
fi

# Parse all options in a single Python invocation
eval "$(python3 -c "
import json, secrets, shlex
opts = json.load(open('$OPTIONS_FILE'))

for k in ('db_host','db_port','db_name','db_user','db_password',
          'grafana_host','grafana_port','grafana_api_key'):
    print(f'export {k.upper()}={shlex.quote(str(opts.get(k, \"\")))}')

print(f'export MCP_PORT={int(opts.get(\"mcp_port\", 8765))}')
print(f'SYNC_INTERVAL={int(opts[\"sync_interval_minutes\"])}')

# Auto-generate MCP API keys for users that don't have one
users = opts['users']
changed = False
for u in users:
    if not u.get('mcp_api_key'):
        u['mcp_api_key'] = secrets.token_urlsafe(32)
        changed = True

print(f'export USERS_JSON={shlex.quote(json.dumps(users))}')

# Persist auto-generated keys back to options.json
if changed:
    json.dump(opts, open('$OPTIONS_FILE', 'w'), indent=2)
    print('echo \"INFO: Auto-generated MCP API keys for users without one\"', flush=True)

# Print API keys so the user can find them in the addon log
for u in users:
    slug = u.get('slug', '?')
    key = u.get('mcp_api_key', '')
    print(f'echo \"  MCP key for {slug}: {key}\"')
")"

echo "=== Health Tracker Addon ==="
echo "DB: ${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "Grafana: ${GRAFANA_HOST}:${GRAFANA_PORT}"
echo "MCP server: port ${MCP_PORT}"
echo "Sync interval: ${SYNC_INTERVAL} minutes"

echo "Running database migrations..."
python3 /app/scripts/run_migrate.py

echo "Provisioning Grafana dashboards..."
python3 /app/scripts/push_dashboards.py || echo "WARN: Grafana provisioning failed (is API key set?)"

# Start MCP server in the background
echo "Starting MCP server on port ${MCP_PORT}..."
python3 /app/scripts/mcp_server.py &
MCP_PID=$!

# Trap signals to cleanly shut down the MCP server
cleanup() {
    echo "Shutting down MCP server (PID ${MCP_PID})..."
    kill "$MCP_PID" 2>/dev/null || true
    wait "$MCP_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

while true; do
    echo ""
    echo "=== Sync cycle started at $(date -Iseconds) ==="

    python3 /app/scripts/run_sync.py || echo "WARN: Sync encountered errors"

    echo "Calculating PMC..."
    python3 /app/scripts/calc_pmc.py || echo "WARN: PMC calculation failed"

    echo "=== Sync cycle complete. Sleeping ${SYNC_INTERVAL} minutes ==="
    sleep $((SYNC_INTERVAL * 60))
done
