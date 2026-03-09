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
          'grafana_host','grafana_port','grafana_api_key',
          'openrouter_api_key','chat_model'):
    print(f'export {k.upper()}={shlex.quote(str(opts.get(k, \"\")))}')

print(f'export MCP_PORT={int(opts.get(\"mcp_port\", 8765))}')
print(f'export CHAT_PORT={int(opts.get(\"chat_port\", 8080))}')
print(f'SYNC_INTERVAL={int(opts[\"sync_interval_minutes\"])}')

chainlit_url = opts.get('chainlit_url', '')
if chainlit_url:
    print(f'export CHAINLIT_URL={shlex.quote(chainlit_url)}')

allow_reg = opts.get('allow_registration', False)
print(f'export ALLOW_REGISTRATION={shlex.quote(\"true\" if allow_reg else \"false\")}')

# OAuth configuration (Google)
gcid = opts.get('google_oauth_client_id', '')
gcsec = opts.get('google_oauth_client_secret', '')
if gcid:
    print(f'export OAUTH_GOOGLE_CLIENT_ID={shlex.quote(gcid)}')
    print(f'export OAUTH_GOOGLE_CLIENT_SECRET={shlex.quote(gcsec)}')

# OAuth configuration (Apple)
acid = opts.get('apple_oauth_client_id', '')
atid = opts.get('apple_oauth_team_id', '')
akid = opts.get('apple_oauth_key_id', '')
akf = opts.get('apple_oauth_private_key_file', '')
if acid:
    print(f'export OAUTH_APPLE_CLIENT_ID={shlex.quote(acid)}')
    print(f'export OAUTH_APPLE_TEAM_ID={shlex.quote(atid)}')
    print(f'export OAUTH_APPLE_KEY_ID={shlex.quote(akid)}')
    if akf:
        key_path = f'/config/{akf}' if not akf.startswith('/') else akf
        print(f'export OAUTH_APPLE_PRIVATE_KEY_FILE={shlex.quote(key_path)}')

# Chainlit auth secret (generate once, persist)
auth_secret = opts.get('_chainlit_auth_secret', '')
if not auth_secret:
    auth_secret = secrets.token_urlsafe(48)
    opts['_chainlit_auth_secret'] = auth_secret
    json.dump(opts, open('$OPTIONS_FILE', 'w'), indent=2)
print(f'export CHAINLIT_AUTH_SECRET={shlex.quote(auth_secret)}')

# Print OAuth status
if gcid:
    print('echo \"  Google OAuth: enabled\"')
if acid:
    print('echo \"  Apple OAuth: enabled\"')
")"

echo "=== Health Coach Addon ==="

# ------------------------------------------------------------------
# Personal config files (athlete.yaml, equipment.yaml, zones.yaml)
# are NOT shipped in the image. They live in /config/healthcoach/
# (HA persistent config dir) and are symlinked into /app/config/ so
# all scripts can find them at their expected paths.
# ------------------------------------------------------------------
HA_CFG=/config/healthcoach
mkdir -p "$HA_CFG"

for cfg in athlete.yaml equipment.yaml zones.yaml; do
    target="$HA_CFG/$cfg"
    link="/app/config/$cfg"

    # Seed from example template on first run
    if [[ ! -f "$target" ]]; then
        example="/app/config/${cfg%.yaml}.example.yaml"
        if [[ -f "$example" ]]; then
            cp "$example" "$target"
            echo "INFO: Created $target from example template -- please customise it"
        fi
    fi

    # Create or refresh the symlink
    ln -sf "$target" "$link"
done
echo "Config files linked from $HA_CFG"

# ------------------------------------------------------------------
# Users are stored in /config/healthcoach/users.json (not options.json)
# so that user credentials survive addon option resets and to avoid
# complex nested-object schemas that the HA supervisor rejects.
# ------------------------------------------------------------------
eval "$(python3 -c "
import json, secrets, shlex
from pathlib import Path

users_file = Path('/config/healthcoach/users.json')

if not users_file.exists():
    users = []
    print('echo \"INFO: users.json not found -- no users configured. Use the chat UI to register.\"')
else:
    users = json.loads(users_file.read_text())

# Auto-generate MCP API keys for users that don't have them
changed = False
for u in users:
    if not u.get('mcp_api_key'):
        u['mcp_api_key'] = secrets.token_urlsafe(32)
        changed = True

if changed:
    users_file.write_text(json.dumps(users, indent=2))
    print('echo \"INFO: Auto-generated MCP API keys for new users\"')

print(f'export USERS_JSON={shlex.quote(json.dumps(users))}')

for u in users:
    slug = u.get('slug', '?')
    email = u.get('email', '')
    key = u.get('mcp_api_key', '')
    print(f'echo \"  User {slug} ({email}): MCP key {key}\"')
")"
echo "DB: ${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "Grafana: ${GRAFANA_HOST}:${GRAFANA_PORT}"
echo "MCP server: port ${MCP_PORT}"
echo "Chat UI: port ${CHAT_PORT} (model: ${CHAT_MODEL})"
echo "Sync interval: ${SYNC_INTERVAL} minutes"

echo "Running database migrations..."
python3 /app/scripts/run_migrate.py

echo "Provisioning Grafana dashboards..."
python3 /app/scripts/push_dashboards.py || echo "WARN: Grafana provisioning failed (is API key set?)"

# Start MCP server in the background
echo "Starting MCP server on port ${MCP_PORT}..."
python3 /app/scripts/mcp_server.py &
MCP_PID=$!

# Start Chainlit chat UI in the background (only if OpenRouter key is set)
CHAT_PID=""
if [[ -n "${OPENROUTER_API_KEY}" ]]; then
    echo "Starting Chainlit chat on port ${CHAT_PORT}..."
    chainlit run /app/scripts/chat_app.py \
        --port "${CHAT_PORT}" --host 0.0.0.0 &
    CHAT_PID=$!
else
    echo "WARN: OPENROUTER_API_KEY not set -- chat UI disabled"
fi

# Trap signals to cleanly shut down background processes
cleanup() {
    echo "Shutting down MCP server (PID ${MCP_PID})..."
    kill "$MCP_PID" 2>/dev/null || true
    if [[ -n "$CHAT_PID" ]]; then
        echo "Shutting down Chainlit chat (PID ${CHAT_PID})..."
        kill "$CHAT_PID" 2>/dev/null || true
        wait "$CHAT_PID" 2>/dev/null || true
    fi
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
