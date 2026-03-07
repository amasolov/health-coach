#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE="/data/options.json"

if [[ ! -f "$OPTIONS_FILE" ]]; then
    echo "ERROR: $OPTIONS_FILE not found -- are we running inside HA?"
    exit 1
fi

# Parse all options in a single Python invocation instead of N separate ones
eval "$(python3 -c "
import json, shlex
opts = json.load(open('$OPTIONS_FILE'))
for k in ('db_host','db_port','db_name','db_user','db_password',
          'grafana_host','grafana_port','grafana_api_key'):
    print(f'export {k.upper()}={shlex.quote(str(opts.get(k, \"\")))}')
print(f'SYNC_INTERVAL={int(opts[\"sync_interval_minutes\"])}')
print(f'export USERS_JSON={shlex.quote(json.dumps(opts[\"users\"]))}')
")"

echo "=== Health Tracker Addon ==="
echo "DB: ${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo "Grafana: ${GRAFANA_HOST}:${GRAFANA_PORT}"
echo "Sync interval: ${SYNC_INTERVAL} minutes"

echo "Running database migrations..."
python3 /app/scripts/run_migrate.py

echo "Provisioning Grafana dashboards..."
python3 /app/scripts/push_dashboards.py || echo "WARN: Grafana provisioning failed (is API key set?)"

while true; do
    echo ""
    echo "=== Sync cycle started at $(date -Iseconds) ==="

    python3 /app/scripts/run_sync.py || echo "WARN: Sync encountered errors"

    echo "Calculating PMC..."
    python3 /app/scripts/calc_pmc.py || echo "WARN: PMC calculation failed"

    echo "=== Sync cycle complete. Sleeping ${SYNC_INTERVAL} minutes ==="
    sleep $((SYNC_INTERVAL * 60))
done
