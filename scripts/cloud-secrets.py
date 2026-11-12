#!/usr/bin/env python3
"""Install pre-existing environment-file secrets through stdin; never put secret values in argv."""

import argparse
import base64
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

MAPPINGS = {
    "abrl-runtime": None,
    "abrl-backup-s3": {
        "ACCESS_KEY_ID": "ABRL_BACKUP_ACCESS_KEY",
        "ACCESS_SECRET_KEY": "ABRL_BACKUP_SECRET_KEY",
        "SSE_CUSTOMER_KEY": "ABRL_BACKUP_SSE_KEY",
    },
    "abrl-data": {
        "redis-password": "ABRL_REDIS_PASSWORD",
        "clickhouse-password": "ABRL_CLICKHOUSE_PASSWORD",
    },
    "abrl-postgres-app": {"username": "=abrl", "password": "ABRL_POSTGRES_PASSWORD"},
    "abrl-postgres-scaler": {"username": "=abrl_scaler", "password": "ABRL_SCALER_PASSWORD"},
    "abrl-metrics": {"token": "ABRL_METRICS_TOKEN"},
    "abrl-scaler-db": {"connection": "ABRL_SCALER_DATABASE_URL"},
    "abrl-hcloud": {"token": "HCLOUD_TOKEN"},
    "hcloud": {"token": "HCLOUD_TOKEN", "network": "HCLOUD_NETWORK"},
}


def validate(data):
    required = {
        value
        for mapping in MAPPINGS.values()
        if mapping
        for value in mapping.values()
        if not value.startswith("=")
    } | {
        "ABRL_DATABASE_URL",
        "ABRL_OIDC_ISSUER",
        "ABRL_OIDC_CLIENT_ID",
        "ABRL_OIDC_CLIENT_SECRET",
        "ABRL_PROFILE_ENCRYPTION_KEY",
        "ABRL_SESSION_SECRET",
        "ABRL_OIDC_OWNER_SUBJECT",
        "ABRL_WEB_ORIGIN",
        "ABRL_CONTROLLED_ORIGIN",
    }
    missing = sorted(k for k in required if not data.get(k))
    if missing:
        raise ValueError("Missing required settings: " + ", ".join(missing))
    if data.get("ABRL_DEPLOYMENT") != "cloud":
        raise ValueError("ABRL_DEPLOYMENT must be cloud")
    for key in required:
        if any(marker in data[key] for marker in ("CHANGE", "YOUR_DOMAIN", "IDENTITY_PROVIDER")):
            raise ValueError("Replace example value for " + key)
    web = urlsplit(data["ABRL_WEB_ORIGIN"])
    controlled = urlsplit(data["ABRL_CONTROLLED_ORIGIN"])
    if (
        web.scheme != "https"
        or not web.hostname
        or web.hostname != controlled.hostname
        or controlled.scheme != "https"
        or any(
            origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
            or origin.port not in (None, 443)
            for origin in (web, controlled)
        )
    ):
        raise ValueError("ABRL_CONTROLLED_ORIGIN must match the HTTPS ABRL_WEB_ORIGIN")
    for key in ("ABRL_SESSION_SECRET", "ABRL_METRICS_TOKEN"):
        if len(data[key]) < 32:
            raise ValueError(key + " requires at least 32 random characters")
    for key in ("ABRL_PROFILE_ENCRYPTION_KEY", "ABRL_BACKUP_SSE_KEY"):
        try:
            decoded = base64.urlsafe_b64decode(data[key])
        except Exception as exc:
            raise ValueError(key + " requires a base64 32-byte key") from exc
        if len(decoded) != 32:
            raise ValueError(key + " requires a base64 32-byte key")


def runtime_values(data):
    excluded = {
        "ABRL_ADSCORE_RESPONSE_KEY",
        "ABRL_SCALER_DATABASE_URL",
        "ABRL_SCALER_PASSWORD",
        "ABRL_POSTGRES_PASSWORD",
    }
    return {
        k: v
        for k, v in data.items()
        if k.startswith(("ABRL_", "OTEL_")) and not k.startswith("ABRL_BACKUP_") and k not in excluded
    }


def load(path):
    if Path(path).stat().st_mode & 0o077:
        raise ValueError("Secret environment file must have mode 0600")
    data = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not name.replace("_", "").isalnum():
            raise ValueError("Invalid environment-file assignment")
        data[name] = value.strip().removeprefix('"').removesuffix('"')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument(
        "--proxy-env-file", help="Separate mode-0600 file with provider reference=credential assignments"
    )
    args = parser.parse_args()
    data = load(args.env_file)
    try:
        validate(data)
    except ValueError as exc:
        parser.error(str(exc))
    mappings = dict(MAPPINGS)
    vendor_keys = [key for key in ("CLOAKBROWSER_LICENSE_KEY", "KAMELEO_PAT") if data.get(key)]
    if vendor_keys:
        mappings["abrl-browser-vendors"] = {key: key for key in vendor_keys}
    verifier_keys = (
        "ABRL_VERIFIER_TOKEN",
        "ABRL_ADSCORE_ZONE_ID",
        "ABRL_ADSCORE_RESPONSE_KEY",
        "ABRL_ADSCORE_ALGORITHM",
    )
    if data.get("ABRL_ADSCORE_RESPONSE_KEY"):
        data.setdefault("ABRL_ADSCORE_ALGORITHM", "v5_0101M")
        if not all(data.get(key) for key in verifier_keys):
            parser.error("AdScore verifier requires token, zone ID and response key")
        mappings["abrl-adscore-verifier"] = {key: key for key in verifier_keys}
    provider_data = (
        {key.upper(): value for key, value in load(args.proxy_env_file).items()}
        if args.proxy_env_file
        else {}
    )
    if provider_data:
        mappings["abrl-proxy-providers"] = {}
    for name, mapping in mappings.items():
        namespace = "kube-system" if name == "hcloud" else "abrl"
        content = (
            runtime_values(data)
            if mapping is None
            else {
                key: value[1:] if value.startswith("=") else data[value] for key, value in mapping.items()
            }
        )
        if name == "abrl-proxy-providers":
            content = provider_data
        document = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace, "labels": {"cnpg.io/reload": "true"}},
            "type": "kubernetes.io/basic-auth" if name.startswith("abrl-postgres-") else "Opaque",
            "stringData": content,
        }
        # kubectl errors are captured so a malformed secret cannot expose values in diagnostics.
        process = subprocess.run(
            ["kubectl", "apply", "-f", "-"], input=json.dumps(document), text=True, capture_output=True
        )
        if process.returncode:
            raise SystemExit(
                "Secret installation failed for "
                + name
                + "; inspect cluster access without displaying secrets."
            )
        print("Installed " + namespace + "/" + name)


if __name__ == "__main__":
    main()
