#!/command/with-contenv bash
# s6-overlay init: parse config, run migrations, set up the addon.
set -euo pipefail

exec python3 /app/scripts/init_addon.py
