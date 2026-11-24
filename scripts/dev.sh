#!/usr/bin/env bash
set -euo pipefail
# Run each service in a process group so shutdown reaches launcher children.
set -m
pids=()
cleanup() {
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  wait || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
api_args=(abrl.api:app)
if [[ ${1:-} == --test-fixtures ]]; then
  api_args=(--app-dir tests --factory browser_fixture:create_app)
fi
uv run uvicorn "${api_args[@]}" --host 127.0.0.1 --port 8400 --no-proxy-headers --no-access-log &
pids+=("$!")
uv run dramatiq abrl.runtime --processes 1 --threads 1 &
pids+=("$!")
uv run python -m abrl.scheduler_service &
pids+=("$!")
uv run python -m abrl.session_runner &
pids+=("$!")
uv run python -m abrl.session_runner --pool vendor &
pids+=("$!")
pnpm --dir apps/web dev &
pids+=("$!")
# bash 3 ships on macOS and lacks wait -n.
while true; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then wait "$pid"; exit 1; fi
  done
  sleep 1
done
