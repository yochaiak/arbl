#!/usr/bin/env bash
# Local static validation only. Does not contact a cloud account or create resources.
set -euo pipefail
cd "$(dirname "$0")/.."
validation_dir=$(mktemp -d)
trap 'rm -rf "$validation_dir"' EXIT
version() { python3 -c 'import json,sys; print(json.load(open("deployment/operators/versions.json"))[sys.argv[1]])' "$1"; }
terraform -chdir=infrastructure/terraform init -backend=false -input=false >/dev/null
terraform -chdir=infrastructure/terraform validate
helm lint deployment/chart
helm template abrl deployment/chart --set cloudWorkers.enabled=true --set cloudWorkers.isolationAttestation=local-template-validation-only --set cloudWorkers.vendorEnabled=true --set postgres.backupsEnabled=true --set postgres.backupEndpoint=https://example.invalid --set postgres.backupDestination=s3://test/ > "$validation_dir/application.yaml"
helm template cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts --version "$(version cnpg_chart)" --namespace cnpg-system --values deployment/operators/cnpg.yaml > "$validation_dir/cnpg.yaml"
helm template barman plugin-barman-cloud --repo https://cloudnative-pg.github.io/charts --version "$(version barman_chart)" --namespace cnpg-system --values deployment/operators/barman.yaml > "$validation_dir/barman.yaml"
helm template keda keda --repo https://kedacore.github.io/charts --version "$(version keda_chart)" --namespace keda --values deployment/operators/keda.yaml > "$validation_dir/keda.yaml"
helm template traefik traefik --repo https://traefik.github.io/charts --version "$(version traefik_chart)" --namespace kube-system --values deployment/operators/traefik.yaml > "$validation_dir/traefik.yaml"
helm template ccm hcloud-cloud-controller-manager --repo https://charts.hetzner.cloud --version "$(version cloud_controller_chart)" --namespace kube-system --values deployment/operators/hcloud-ccm.yaml > "$validation_dir/ccm.yaml"
helm template csi hcloud-csi --repo https://charts.hetzner.cloud --version "$(version csi_chart)" --namespace kube-system --values deployment/operators/hcloud-csi.yaml > "$validation_dir/csi.yaml"
uv run --with jsonschema python - "$validation_dir" <<'PY'
import sys
from pathlib import Path
import jsonschema
import yaml

directory = Path(sys.argv[1])
schemas = {}
for name in ("cnpg", "barman", "keda"):
    for resource in yaml.safe_load_all((directory / f"{name}.yaml").read_text()):
        if resource and resource.get("kind") == "CustomResourceDefinition":
            spec = resource["spec"]
            for version in spec["versions"]:
                schemas[(spec["group"] + "/" + version["name"], spec["names"]["kind"])] = version["schema"]["openAPIV3Schema"]
count = 0
for resource in yaml.safe_load_all((directory / "application.yaml").read_text()):
    if resource and (resource["apiVersion"], resource["kind"]) in schemas:
        jsonschema.validate(resource, schemas[(resource["apiVersion"], resource["kind"])])
        count += 1
print(f"Validated {count} custom resources against pinned official CRD schemas")
PY
uv run python -m pytest -q tests/test_infrastructure.py
