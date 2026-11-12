#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v helm >/dev/null
command -v kubectl >/dev/null
version() { python3 -c 'import json,sys; print(json.load(open("deployment/operators/versions.json"))[sys.argv[1]])' "$1"; }
# All credentials are pre-existing Secrets; Helm values and release history contain none.
kubectl -n kube-system get secret hcloud >/dev/null
helm upgrade --install hcloud-ccm hcloud-cloud-controller-manager --repo https://charts.hetzner.cloud --version "$(version cloud_controller_chart)" --namespace kube-system --values deployment/operators/hcloud-ccm.yaml --wait --timeout 10m
helm upgrade --install hcloud-csi hcloud-csi --repo https://charts.hetzner.cloud --version "$(version csi_chart)" --namespace kube-system --values deployment/operators/hcloud-csi.yaml --wait --timeout 10m
helm upgrade --install cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts --version "$(version cnpg_chart)" --namespace cnpg-system --create-namespace --values deployment/operators/cnpg.yaml --wait --timeout 10m
helm upgrade --install barman plugin-barman-cloud --repo https://cloudnative-pg.github.io/charts --version "$(version barman_chart)" --namespace cnpg-system --values deployment/operators/barman.yaml --wait --timeout 10m
helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version "$(version keda_chart)" --namespace keda --create-namespace --values deployment/operators/keda.yaml --wait --timeout 10m
helm upgrade --install traefik traefik --repo https://traefik.github.io/charts --version "$(version traefik_chart)" --namespace kube-system --values deployment/operators/traefik.yaml --wait --timeout 10m
