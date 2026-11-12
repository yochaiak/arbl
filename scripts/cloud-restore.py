#!/usr/bin/env python3
"""Create a NEW CNPG cluster from native SSE-C Barman backups; never overwrite the source."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def restore_manifest(name, endpoint, destination, secret, source_server="abrl-postgres", target_time=None):
    if name == source_server or not re.fullmatch(r"[a-z][a-z0-9-]{2,50}", name):
        raise ValueError("Restore requires a new DNS-safe cluster name")
    if not endpoint.startswith("https://") or not destination.startswith("s3://"):
        raise ValueError("Use HTTPS and an s3:// native backup destination")
    store_name = name + "-source"
    recovery = {"source": "recovery-source"}
    if target_time:
        recovery["recoveryTarget"] = {"targetTime": target_time}
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "barmancloud.cnpg.io/v1",
                "kind": "ObjectStore",
                "metadata": {"name": store_name, "namespace": "abrl"},
                "spec": {
                    "configuration": {
                        "destinationPath": destination,
                        "endpointURL": endpoint,
                        "s3Credentials": {
                            "accessKeyId": {"name": secret, "key": "ACCESS_KEY_ID"},
                            "secretAccessKey": {"name": secret, "key": "ACCESS_SECRET_KEY"},
                            "sseCustomerKey": {"name": secret, "key": "SSE_CUSTOMER_KEY"},
                        },
                    }
                },
            },
            {
                "apiVersion": "postgresql.cnpg.io/v1",
                "kind": "Cluster",
                "metadata": {"name": name, "namespace": "abrl"},
                "spec": {
                    "instances": 3,
                    "imageName": "ghcr.io/cloudnative-pg/postgresql:16.13",
                    "bootstrap": {"recovery": recovery},
                    "externalClusters": [
                        {
                            "name": "recovery-source",
                            "plugin": {
                                "name": "barman-cloud.cloudnative-pg.io",
                                "parameters": {"barmanObjectName": store_name, "serverName": source_server},
                            },
                        }
                    ],
                    "postgresql": {
                        "synchronous": {"method": "any", "number": 1, "dataDurability": "required"}
                    },
                    "storage": {"size": "200Gi", "storageClass": "hcloud-volumes"},
                    "affinity": {
                        "enablePodAntiAffinity": True,
                        "podAntiAffinityType": "required",
                        "topologyKey": "kubernetes.io/hostname",
                        "nodeSelector": {"abrl.io/role": "data"},
                        "tolerations": [
                            {
                                "key": "abrl.io/data",
                                "value": "true",
                                "operator": "Equal",
                                "effect": "NoSchedule",
                            }
                        ],
                    },
                },
            },
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-server", default="abrl-postgres")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--secret", default="abrl-backup-s3")
    parser.add_argument("--target-time")
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    document = restore_manifest(
        args.name, args.endpoint, args.destination, args.secret, args.source_server, args.target_time
    )
    path = Path(args.output)
    if path.exists():
        parser.error("Choose a new recovery manifest filename")
    path.write_text(json.dumps(document, indent=2) + "\n")
    if args.apply:
        check = subprocess.run(
            ["kubectl", "-n", "abrl", "get", "cluster", args.name, "--ignore-not-found", "-o", "name"],
            capture_output=True,
            text=True,
            check=True,
        )
        if check.stdout.strip():
            parser.error("Cluster already exists; restore must create a new cluster")
        subprocess.run(["kubectl", "apply", "-f", str(path)], check=True)
    print(
        "Prepared new-cluster recovery. The source backup remains read-only; configure a NEW archive destination before admitting sessions."
    )


if __name__ == "__main__":
    main()
