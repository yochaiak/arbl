#!/usr/bin/env python3
"""Bootstrap existing Terraform nodes. Never creates a server or puts secrets in state."""

import argparse
import base64
import hashlib
import ipaddress
import json
import shlex
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = json.loads((ROOT / "deployment/operators/versions.json").read_text())


def inventory(path):
    data = json.loads(Path(path).read_text())
    return data["inventory"]["value"] if "inventory" in data else data


def remote(address, script, *, identity=None):
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    if identity:
        args += ["-i", identity]
    return subprocess.run(
        args + ["root@" + address, "sh", "-se"], input=script, text=True, check=True, capture_output=True
    ).stdout


def k3s_config(node, controls, token, first=False):
    config = {
        "token": token,
        "node-ip": node["private_address"],
        "node-external-ip": node["address"],
        "node-label": ["abrl.io/role=" + node["role"]],
        "kubelet-arg": ["cloud-provider=external"],
    }
    if node["role"] == "control":
        config.update(
            {
                "secrets-encryption": True,
                "advertise-address": node["private_address"],
                "disable": ["servicelb", "traefik", "local-storage"],
                "disable-cloud-controller": True,
                "flannel-backend": "wireguard-native",
                "tls-san": [n["address"] for n in controls] + [n["private_address"] for n in controls],
                "etcd-snapshot-schedule-cron": "*/5 * * * *",
                "etcd-snapshot-retention": 12,
                "node-taint": ["node-role.kubernetes.io/control-plane=true:NoSchedule"],
            }
        )
        if first:
            config["cluster-init"] = True
        else:
            config["server"] = "https://" + controls[0]["private_address"] + ":6443"
    else:
        config["server"] = "https://" + controls[0]["private_address"] + ":6443"
        if node["role"] == "data":
            config["node-taint"] = ["abrl.io/data=true:NoSchedule"]
    return config


def worker_cloud_init(pool, controls, token, network_cidr="10.20.0.0/16"):
    """K3s credentials are stored in an encrypted K8s Secret, not in Terraform."""
    version = VERSIONS["k3s"]
    servers = " ".join(shlex.quote("https://" + n["private_address"] + ":6443") for n in controls)
    config = {
        "token": token,
        "node-label": ["abrl.io/role=browser", "abrl.io/pool=" + pool],
        "node-taint": ["abrl.io/browser=true:NoSchedule"],
        "kubelet-arg": ["cloud-provider=external"],
    }
    # JSON is valid YAML; prevents injection through join tokens or labels.
    payload = base64.b64encode(json.dumps(config).encode()).decode()
    address_code = (
        "import ipaddress,json,subprocess; n=ipaddress.ip_network("
        + repr(network_cidr)
        + "); print(next(a['local'] for i in json.loads(subprocess.check_output(['ip','-j','-4','addr'])) for a in i['addr_info'] if ipaddress.ip_address(a['local']) in n))"
    )
    command = f"""set -eu
install -d -m 700 /etc/rancher/k3s
printf %s {shlex.quote(payload)} | base64 -d > /etc/rancher/k3s/config.yaml
chmod 600 /etc/rancher/k3s/config.yaml
export K3S_NODE_IP="$(python3 -c {shlex.quote(address_code)})"
for endpoint in {servers}; do
  if curl -ksSf --connect-timeout 4 "$endpoint/ping" >/dev/null; then
    export K3S_URL="$endpoint"; break
  fi
done
: "${{K3S_URL:?No control-plane endpoint is reachable}}"
curl -fsSL 'https://raw.githubusercontent.com/k3s-io/k3s/{version}/install.sh' -o /root/install-k3s.sh
printf '%s  %s\\n' '{VERSIONS["k3s_installer_sha256"]}' /root/install-k3s.sh | sha256sum -c -
INSTALL_K3S_VERSION={shlex.quote(version)} INSTALL_K3S_EXEC=agent sh /root/install-k3s.sh
rm /root/install-k3s.sh
"""
    return "#cloud-config\n" + json.dumps(
        {
            "package_update": True,
            "packages": ["curl", "ca-certificates", "wireguard-tools"],
            "write_files": [
                {
                    "path": "/var/lib/kubelet/seccomp/abrl-chromium.json",
                    "permissions": "0644",
                    "encoding": "b64",
                    "content": base64.b64encode(
                        (ROOT / "infrastructure/docker/chromium-seccomp.json").read_bytes()
                    ).decode(),
                },
                {
                    "path": "/root/abrl-join.sh",
                    "permissions": "0700",
                    "encoding": "b64",
                    "content": base64.b64encode(command.encode()).decode(),
                },
            ],
            "runcmd": [["sh", "/root/abrl-join.sh"], ["rm", "-f", "/root/abrl-join.sh"]],
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--identity")
    parser.add_argument(
        "--join-token-file", required=True, help="Existing recovery-managed random token (mode 0600)"
    )
    parser.add_argument(
        "--autoscaler-secret",
        action="store_true",
        help="Install worker cloud-init into encrypted Kubernetes Secret after bootstrap",
    )
    parser.add_argument("--network-cidr", default="10.20.0.0/16")
    parser.add_argument("--firewall-id")
    parser.add_argument("--cluster-name", default="abrl")
    args = parser.parse_args()
    token_path = Path(args.join_token_file)
    if token_path.stat().st_mode & 0o077:
        parser.error("join-token-file must have mode 0600")
    token = token_path.read_text().strip()
    if len(token) < 32:
        parser.error("join token must be at least 32 characters")
    nodes = inventory(args.inventory)
    controls = [n for _, n in sorted(nodes.items()) if n["role"] == "control"]
    if len(controls) != 3:
        parser.error("exactly three control nodes required")
    if args.autoscaler_secret:
        if not args.firewall_id:
            parser.error("Autoscaled nodes require the foundation firewall ID")
        configs = {}
        subnets = list(ipaddress.ip_network(args.network_cidr).subnets(prefixlen_diff=4))
        for pool in ["standard", "vendor"]:
            configs[pool] = {
                "cloudInit": base64.b64encode(
                    worker_cloud_init(pool, controls, token, args.network_cidr).encode()
                ).decode(),
                "labels": {"abrl.io/role": "browser", "abrl.io/pool": pool},
                "subnetIPRange": str(subnets[1 if pool == "standard" else 2]),
                "serverLabels": {"cluster": args.cluster_name, "owner": "autoscaler", "pool": pool},
                "taints": [{"key": "abrl.io/browser", "value": "true", "effect": "NoSchedule"}],
            }
            if args.firewall_id:
                configs[pool]["firewalls"] = [args.firewall_id]
        config = {
            "imagesForArch": {"amd64": "ubuntu-24.04"},
            "defaultSubnetIPRange": str(subnets[1]),
            "nodeConfigs": configs,
        }
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "abrl-autoscaler-config", "namespace": "abrl"},
            "stringData": {"config.json": json.dumps(config)},
        }
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=json.dumps(secret),
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        print(
            "Installed encrypted-at-rest autoscaler configuration; no Terraform state contains join tokens."
        )
        return
    url = "https://raw.githubusercontent.com/k3s-io/k3s/" + VERSIONS["k3s"] + "/install.sh"
    installer = urllib.request.urlopen(url, timeout=30).read()
    if hashlib.sha256(installer).hexdigest() != VERSIONS["k3s_installer_sha256"]:
        raise SystemExit("K3s installer checksum mismatch")
    ordered = sorted(nodes.items(), key=lambda item: (item[1]["role"] != "control", item[0]))
    for index, (name, node) in enumerate(ordered):
        cfg = base64.b64encode(
            json.dumps(k3s_config(node, controls, token, first=index == 0)).encode()
        ).decode()
        encoded = base64.b64encode(installer).decode()
        role = "server" if node["role"] == "control" else "agent"
        script = f"""umask 077
install -d -m 700 /etc/rancher/k3s
printf %s {shlex.quote(cfg)} | base64 -d > /etc/rancher/k3s/config.yaml
printf %s {shlex.quote(encoded)} | base64 -d > /root/install-k3s.sh
INSTALL_K3S_VERSION={shlex.quote(VERSIONS["k3s"])} INSTALL_K3S_EXEC={role} sh /root/install-k3s.sh >/dev/null 2>&1
rm /root/install-k3s.sh
"""
        try:
            remote(node["address"], script, identity=args.identity)
        except subprocess.CalledProcessError:
            raise SystemExit(
                f"Bootstrap failed on {name}; inspect that host locally (remote logs withheld to protect secrets)."
            ) from None
        print(f"Bootstrapped {name}")
    print(
        "Retrieve kubeconfig through verified SSH; replace its server with a control-node public address. Keep join token and K3s encryption keys in external recovery storage."
    )


if __name__ == "__main__":
    main()
