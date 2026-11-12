"""Per-session Kubernetes browser/broker ownership for Chromium sessions.

Application pods keep database credentials; browser and egress pods never receive
those secrets. No profile is considered released until both runtime pod UIDs are
confirmed absent. This module does not assert that a cloud deployment was tested.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx


@dataclass
class RemoteRuntime:
    cdp_url: str
    proxy_url: str
    metadata: dict
    token: str = field(repr=False)
    session_id: str = ""
    epoch: int = 0
    stopped: bool = False

    @property
    def connection(self):
        return {
            "browser_url": self.cdp_url,
            "proxy_url": self.proxy_url,
            "token": self.token,
            "session_id": self.session_id,
            "epoch": self.epoch,
        }


class CloudRuntime:
    def __init__(self, session_id: str, epoch: int, *, client=None, namespace=None, image=None):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", session_id) or epoch < 1:
            raise ValueError("Invalid runtime identity")
        self.session_id = session_id
        self.epoch = epoch
        self.namespace = namespace or os.getenv("ABRL_RUNTIME_NAMESPACE", "abrl")
        self.image = image or os.environ.get("ABRL_BROWSER_IMAGE")
        self.explicit_image = image
        self.adapter = "playwright"
        self.prefix = "br-" + session_id.replace("-", "").lower()[:24] + "-" + str(epoch)
        self.token = secrets.token_urlsafe(32)
        self.created: list[tuple[str, str, str | None]] = []
        self.client = client
        self._own_client = client is None
        self.owner = None
        self.runner_label = None

    async def connect(self):
        if self.client is None:
            directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
            context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
            token = (directory / "token").read_text().strip()
            self.client = httpx.AsyncClient(
                base_url="https://"
                + os.environ["KUBERNETES_SERVICE_HOST"]
                + ":"
                + os.getenv("KUBERNETES_SERVICE_PORT", "443"),
                headers={"Authorization": "Bearer " + token},
                verify=context,
                timeout=20,
            )
        pod_name = os.environ.get("ABRL_POD_NAME")
        if not pod_name:
            raise ValueError("Cloud runner needs downward-API ABRL_POD_NAME")
        pod = await self.request("GET", self.path("pods", pod_name))
        if pod["metadata"].get("labels", {}).get("abrl.io/role") != "runner":
            raise ValueError("Remote browser provisioning requires a runner workload")
        job = next((o for o in pod["metadata"].get("ownerReferences", []) if o["kind"] == "Job"), None)
        if not job:
            raise ValueError("Remote browser runner must belong to a Kubernetes Job")
        self.owner = {**job, "controller": False, "blockOwnerDeletion": False}
        self.runner_label = {"batch.kubernetes.io/job-name": job["name"]}

    def path(self, kind, name=None):
        group = "/apis/networking.k8s.io/v1" if kind == "networkpolicies" else "/api/v1"
        return (
            group
            + "/namespaces/"
            + quote(self.namespace, safe="")
            + "/"
            + kind
            + (("/" + quote(name, safe="")) if name else "")
        )

    async def request(self, method, path, data=None, missing_ok=False):
        response = await self.client.request(method, path, json=data)
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 400:
            # Kubernetes responses can echo submitted environment data; do not log them.
            raise RuntimeError(f"Kubernetes runtime operation {method} failed ({response.status_code})")
        return response.json()

    async def create(self, kind, body):
        metadata = body["metadata"]
        metadata["namespace"] = self.namespace
        metadata["ownerReferences"] = [self.owner]
        # Never adopt an existing runtime on HTTP 409. An older owner must be fenced.
        result = await self.request("POST", self.path(kind), body)
        self.created.append((kind, metadata["name"], result["metadata"].get("uid")))
        return result

    def labels(self, role):
        return {"abrl.io/role": role, "abrl.io/session": self.prefix, "abrl.io/epoch": str(self.epoch)}

    def pod(self, role, command, env, ports, timeout_seconds):
        name = self.prefix + "-" + role
        spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": timeout_seconds,
            "terminationGracePeriodSeconds": 30,
            "nodeSelector": {
                "abrl.io/pool": "standard" if self.adapter in {"playwright", "manual_chrome"} else "vendor",
                "kubernetes.io/arch": "amd64",
            },
            "tolerations": [
                {"key": "abrl.io/browser", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
            ],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": (
                    {"type": "Localhost", "localhostProfile": "abrl-chromium.json"}
                    if role == "browser"
                    else {"type": "RuntimeDefault"}
                ),
            },
            "containers": [
                {
                    "name": role,
                    "image": self.image,
                    "command": (
                        ["/app/scripts/browser-entrypoint.sh", *command]
                        if role == "browser" and self.adapter == "manual_chrome"
                        else command
                    ),
                    "env": env,
                    "ports": [{"name": name, "containerPort": port} for name, port in ports],
                    "readinessProbe": {"tcpSocket": {"port": ports[0][1]}, "periodSeconds": 2},
                    "resources": {
                        "requests": {
                            "cpu": ("2" if self.adapter in {"playwright", "manual_chrome"} else "4")
                            if role == "browser"
                            else "100m",
                            "memory": ("4Gi" if self.adapter in {"playwright", "manual_chrome"} else "8Gi")
                            if role == "browser"
                            else "128Mi",
                        },
                        "limits": {
                            "cpu": ("2" if self.adapter in {"playwright", "manual_chrome"} else "4")
                            if role == "browser"
                            else "500m",
                            "memory": ("4Gi" if self.adapter in {"playwright", "manual_chrome"} else "8Gi")
                            if role == "browser"
                            else "512Mi",
                        },
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": "tmp", "mountPath": "/tmp"},
                        {"name": "shm", "mountPath": "/dev/shm"},
                    ],
                }
            ],
            "volumes": [
                {"name": "tmp", "emptyDir": {"sizeLimit": "4Gi"}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
            ],
        }
        pull_secrets = json.loads(os.getenv("ABRL_IMAGE_PULL_SECRETS", "[]"))
        if not isinstance(pull_secrets, list) or any(
            not isinstance(item, dict)
            or set(item) != {"name"}
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", item["name"])
            for item in pull_secrets
        ):
            raise ValueError(
                "ABRL_IMAGE_PULL_SECRETS must contain only named Kubernetes image-pull secrets"
            )
        spec["imagePullSecrets"] = pull_secrets
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "labels": self.labels(role),
                "annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"},
            },
            "spec": spec,
        }

    async def service(self, role, ports):
        return await self.create(
            "services",
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": self.prefix + "-" + role},
                "spec": {
                    "selector": self.labels(role),
                    "ports": [{"name": name, "port": port, "targetPort": port} for name, port in ports],
                },
            },
        )

    async def policies(self):
        browser = {"matchLabels": self.labels("browser")}
        broker = {"matchLabels": self.labels("broker")}
        # Global policy grants neither browser ingress nor broker ingress: these
        # session selectors are the only ingress grants.
        for role, ingress, egress in [
            (
                "browser",
                [
                    {
                        "from": [{"podSelector": {"matchLabels": self.runner_label}}],
                        "ports": [{"port": 9222}],
                    }
                ],
                [{"to": [{"podSelector": broker}], "ports": [{"port": 3128}]}],
            ),
            (
                "broker",
                [
                    {"from": [{"podSelector": browser}], "ports": [{"port": 3128}]},
                    {
                        "from": [{"podSelector": {"matchLabels": self.runner_label}}],
                        "ports": [{"port": 3128}],
                    },
                ],
                None,
            ),
        ]:
            spec = {
                "podSelector": {"matchLabels": self.labels(role)},
                "policyTypes": ["Ingress"],
                "ingress": ingress,
            }
            if egress is not None:
                spec.update(policyTypes=["Ingress", "Egress"], egress=egress)
            await self.create(
                "networkpolicies",
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": {"name": self.prefix + "-" + role},
                    "spec": spec,
                },
            )

    async def wait_running(self, name, deadline):
        while time.monotonic() < deadline:
            pod = await self.request("GET", self.path("pods", name))
            phase = pod.get("status", {}).get("phase")
            if (
                phase == "Running"
                and all(c.get("ready") for c in pod["status"].get("containerStatuses", []))
                and pod["status"].get("containerStatuses")
            ):
                return {
                    "pod_uid": pod["metadata"].get("uid"),
                    "node": pod.get("spec", {}).get("nodeName"),
                    "containers": [
                        {"name": c.get("name"), "image": c.get("image"), "image_id": c.get("imageID")}
                        for c in pod["status"]["containerStatuses"]
                    ],
                }
            if phase in {"Failed", "Succeeded"}:
                raise RuntimeError("Remote runtime exited before becoming ready")
            await asyncio.sleep(1)
        raise TimeoutError("Cloud browser startup exceeded admitted execution budget")

    async def stop(self):
        confirmed = True
        for kind, name, uid in reversed(self.created):
            try:
                options = {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": 20}
                if uid:
                    options["preconditions"] = {"uid": uid}
                await self.request("DELETE", self.path(kind, name), options, missing_ok=True)
            except Exception:
                confirmed = False
        deadline = time.monotonic() + 60
        for kind, name, uid in self.created:
            if kind != "pods":
                continue
            try:
                while time.monotonic() < deadline:
                    remaining = await self.request("GET", self.path(kind, name), missing_ok=True)
                    if remaining is None:
                        break
                    if remaining["metadata"].get("uid") != uid:
                        # A foreign replacement at the same name is never our owned runtime.
                        break
                    await asyncio.sleep(1)
                else:
                    confirmed = False
            except Exception:
                confirmed = False
        if self._own_client and self.client:
            await self.client.aclose()
        return confirmed

    @asynccontextmanager
    async def launch(self, config):
        self.adapter = config.get("adapter", "playwright")
        image_variables = {
            "playwright": "ABRL_BROWSER_IMAGE",
            "manual_chrome": "ABRL_BROWSER_IMAGE_MANUAL_CHROME",
            "cloakbrowser": "ABRL_CLOAK_BROWSER_IMAGE",
            "kameleo": "ABRL_KAMELEO_BROWSER_IMAGE",
        }
        if self.adapter not in image_variables:
            raise ValueError("Cloud remote runtime does not support this adapter")
        self.image = self.explicit_image or os.getenv(image_variables[self.adapter])
        if self.adapter == "kameleo" and not re.search(
            r"@sha256:[a-f0-9]{64}$", os.getenv("ABRL_KAMELEO_ENGINE_IMAGE", "")
        ):
            raise ValueError("Cloud Kameleo requires a digest-pinned engine sidecar image")
        if not self.image:
            raise ValueError("The adapter requires a configured tested remote-browser image")
        runtime = None
        try:
            await self.connect()
            await self.policies()
            await self.create(
                "secrets",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": self.prefix + "-control"},
                    "stringData": {
                        name: hmac.new(self.token.encode(), label.encode(), hashlib.sha256).hexdigest()
                        for name, label in (
                            ("browser-token", "abrl-browser-control"),
                            ("broker-token", "abrl-broker-control"),
                            ("proxy-token", "abrl-proxy-access"),
                        )
                    },
                },
            )
            common_env = [
                {
                    "name": "ABRL_REMOTE_PROXY_TOKEN",
                    "valueFrom": {"secretKeyRef": {"name": self.prefix + "-control", "key": "proxy-token"}},
                },
                {
                    "name": "ABRL_REMOTE_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {"name": self.prefix + "-control", "key": "broker-token"}
                    },
                },
                {"name": "ABRL_REMOTE_SESSION_ID", "value": self.session_id},
                {"name": "ABRL_REMOTE_EPOCH", "value": str(self.epoch)},
            ]
            broker_svc = await self.service("broker", [("proxy", 3128)])
            broker_ip = broker_svc["spec"]["clusterIP"]
            broker_config = {
                "origin": config["origin"],
                "traffic_budget_bytes": config.get("traffic_budget_bytes", 104857600),
                "resource_origins": config.get("resource_origins", []),
                "navigation_origins": config.get("navigation_origins", []),
                "proxy": config.get("proxy"),
                "vendor_control_origins": (
                    ["https://cloakbrowser.dev"]
                    if self.adapter == "cloakbrowser"
                    else json.loads(os.getenv("ABRL_KAMELEO_CONTROL_ORIGINS_JSON", "[]"))
                    if self.adapter == "kameleo"
                    else []
                ),
            }
            if self.adapter == "kameleo" and not broker_config["vendor_control_origins"]:
                raise ValueError(
                    "Configure explicit documented Kameleo licensing origins before remote execution"
                )
            broker_env = [
                *common_env,
                {"name": "ABRL_EGRESS_CONFIG_JSON", "value": json.dumps(broker_config)},
            ]
            # Only selected provider credential references enter the broker, never DB secrets.
            proxy = config.get("proxy") or {}
            for field in ("username_ref", "password_ref"):
                reference = proxy.get(field)
                if reference:
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", reference):
                        raise ValueError("Proxy credentials require named environment references")
                    broker_env.append(
                        {
                            "name": "ABRL_SECRET_" + reference.upper(),
                            "valueFrom": {
                                "secretKeyRef": {"name": "abrl-proxy-providers", "key": reference.upper()}
                            },
                        }
                    )
            lifetime = min(86400, max(60, int(config.get("timeout_seconds", 600)) + 360))
            broker_env.append({"name": "ABRL_REMOTE_LIFETIME_SECONDS", "value": str(lifetime)})
            await self.create(
                "pods",
                self.pod(
                    "broker",
                    ["python", "-m", "abrl.remote_browser", "broker"],
                    broker_env,
                    [("proxy", 3128)],
                    lifetime,
                ),
            )
            browser_svc = await self.service("browser", [("cdp", 9222)])
            proxy_url = "http://" + broker_ip + ":3128"
            browser_env = [
                *[
                    {
                        **entry,
                        "valueFrom": {
                            "secretKeyRef": {"name": self.prefix + "-control", "key": "browser-token"}
                        },
                    }
                    if entry["name"] == "ABRL_REMOTE_TOKEN"
                    else entry
                    for entry in common_env
                ],
                {"name": "ABRL_REMOTE_ADAPTER", "value": self.adapter},
                {"name": "ABRL_REMOTE_PROXY_URL", "value": proxy_url},
                {"name": "ABRL_REMOTE_LIFETIME_SECONDS", "value": str(lifetime)},
            ]
            if self.adapter == "cloakbrowser":
                browser_env.append(
                    {
                        "name": "CLOAKBROWSER_LICENSE_KEY",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": "abrl-browser-vendors",
                                "key": "CLOAKBROWSER_LICENSE_KEY",
                            }
                        },
                    }
                )
                for key in ("CLOAKBROWSER_VERSION", "ABRL_CLOAK_TIER"):
                    if os.getenv(key):
                        browser_env.append({"name": key, "value": os.environ[key]})
            if self.adapter == "kameleo":
                browser_env.append({"name": "ABRL_KAMELEO_ENDPOINT", "value": "http://127.0.0.1:5050"})
            browser_pod = self.pod(
                "browser",
                ["python", "-m", "abrl.remote_browser", "browser"],
                browser_env,
                [("cdp", 9222)],
                lifetime,
            )
            if self.adapter == "kameleo":
                browser_pod["spec"]["securityContext"].update(runAsUser=1001, runAsGroup=1001, fsGroup=1001)
                browser_pod["spec"]["containers"][0]["resources"] = {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "1", "memory": "512Mi"},
                }
                browser_pod["spec"]["volumes"].append(
                    {"name": "engine-data", "emptyDir": {"sizeLimit": "30Gi"}}
                )
                authenticated_proxy = (
                    "http://abrl-session:$(ABRL_REMOTE_PROXY_TOKEN)@" + broker_ip + ":3128"
                )
                browser_pod["spec"]["containers"].append(
                    {
                        "name": "kameleo-engine",
                        "image": os.environ["ABRL_KAMELEO_ENGINE_IMAGE"],
                        "env": [
                            {
                                "name": "PAT",
                                "valueFrom": {
                                    "secretKeyRef": {"name": "abrl-browser-vendors", "key": "KAMELEO_PAT"}
                                },
                            },
                            common_env[0],
                            {"name": "HTTP_PROXY", "value": authenticated_proxy},
                            {"name": "HTTPS_PROXY", "value": authenticated_proxy},
                            {"name": "NO_PROXY", "value": "127.0.0.1,localhost"},
                        ],
                        "readinessProbe": {
                            "httpGet": {"path": "/general/healthcheck", "port": 5050},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                        },
                        "resources": {
                            "requests": {"cpu": "4", "memory": "8Gi"},
                            "limits": {"cpu": "4", "memory": "8Gi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {"name": "tmp", "mountPath": "/tmp"},
                            {"name": "shm", "mountPath": "/dev/shm"},
                            {"name": "engine-data", "mountPath": "/data"},
                        ],
                    }
                )
            await self.create("pods", browser_pod)
            deadline = time.monotonic() + int(config.get("startup_timeout_seconds", 180))
            broker_metadata = await self.wait_running(self.prefix + "-broker", deadline)
            browser_metadata = await self.wait_running(self.prefix + "-browser", deadline)
            runtime = RemoteRuntime(
                "http://" + browser_svc["spec"]["clusterIP"] + ":9222",
                proxy_url,
                {
                    "runtime": "kubernetes_remote",
                    "session": self.session_id,
                    "epoch": self.epoch,
                    "broker": broker_metadata,
                    "browser": browser_metadata,
                },
                self.token,
                self.session_id,
                self.epoch,
            )
            yield runtime
        finally:
            stopped = await self.stop()
            if runtime:
                runtime.stopped = stopped
