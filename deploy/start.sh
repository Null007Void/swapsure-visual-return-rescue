#!/usr/bin/env bash
# Start SwapSure behind Caddy on a single VM with a persistent disk.
set -euo pipefail

: "${JUDGE_HOST:?set JUDGE_HOST to the public hostname}"
: "${JUDGE_BASIC_AUTH_HASH:?set JUDGE_BASIC_AUTH_HASH from caddy hash-password}"
DATA_DIR="${DATA_DIR:-/mnt/swapsure-data}"

mkdir -p "$DATA_DIR"

export SWAPSURE_PREVIEW_MODE=LIVE
export SWAPSURE_EXTERNAL_SCHEME=https
export SWAPSURE_PUBLIC_HOST="$JUDGE_HOST"
export SWAPSURE_THREADED=1
export SWAPSURE_JUDGING_NOTICE=1
# The judging ledger is separate from any recording ledger and lives on the persistent disk.
export SWAPSURE_LIVE_LEDGER="$DATA_DIR/judging-ledger.sqlite3"
export SWAPSURE_LIVE_AUTHORIZATION_REF="${SWAPSURE_LIVE_AUTHORIZATION_REF:-JUDGING-DEMO}"

cd /app
python src/ui/server.py --port 8080 --data-dir "$DATA_DIR" \
  --preview-mode LIVE --bind 127.0.0.1 --public-host "$JUDGE_HOST" \
  --external-scheme https --threaded &
exec caddy run --config deploy/Caddyfile
