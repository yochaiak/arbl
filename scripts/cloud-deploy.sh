#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then echo "Usage: $0 /path/to/filled-values.yaml" >&2; exit 2; fi
values="$1"
kubectl -n abrl get secret abrl-runtime abrl-postgres-app abrl-postgres-scaler abrl-scaler-db abrl-data abrl-metrics >/dev/null
# Create data services before the migration hook. Subsequent upgrades keep existing
# application replicas while the pre-upgrade migration finishes.
if ! helm status abrl -n abrl >/dev/null 2>&1; then
  helm upgrade --install abrl deployment/chart -n abrl --create-namespace -f "$values" --set application.enabled=false --set cloudWorkers.enabled=false --set autoscaler.enabled=false --timeout 15m
fi
kubectl -n abrl wait cluster/abrl-postgres --for=condition=Ready --timeout=900s
helm upgrade abrl deployment/chart -n abrl -f "$values" --set application.enabled=true --wait --atomic --timeout 15m
kubectl -n abrl rollout status deployment/abrl-api --timeout=180s
kubectl -n abrl rollout status deployment/abrl-scheduler --timeout=180s
