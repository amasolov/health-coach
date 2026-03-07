#!/usr/bin/env bash
# Wrapper that loads .env before launching an MCP server.
# Usage: mcp-env.sh <command> [args...]
#
# If .env doesn't exist, attempts to generate it from sops-encrypted secrets.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="$PROJECT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  SECRETS_ENC="$PROJECT_DIR/secrets.enc.yaml"
  if [[ -f "$SECRETS_ENC" ]]; then
    cd "$PROJECT_DIR" && task secrets:env 2>/dev/null || true
  fi
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

exec "$@"
