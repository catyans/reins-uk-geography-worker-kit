#!/usr/bin/env bash
set -euo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$kit_dir"

: "${UK_GEO_ENTRYPOINT:?Set UK_GEO_ENTRYPOINT to the Agent module:function}"
: "${REINS_EXPERIMENT_WORKER_TOKEN_FILE:?Set the private experiment worker token file path}"

export PYTHONPATH="$kit_dir${PYTHONPATH:+:$PYTHONPATH}"
exec reins control \
  --url "${REINS_CONTROL_URL:-https://47.245.114.167:8444}" \
  --token-file "$REINS_EXPERIMENT_WORKER_TOKEN_FILE" \
  experiment-worker \
  --adapter uk-geography-v1 \
  --callable my_agent:run \
  --worker-id "${REINS_WORKER_ID:-uk-geography-worker}" \
  --state "${REINS_WORKER_STATE:-$kit_dir/worker-state}" \
  "$@"
