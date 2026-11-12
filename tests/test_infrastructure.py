"""Infrastructure invariants and cryptographic recovery tests; no paid resources."""

import base64
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"cloud-{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = script("backup")
preflight = script("preflight")
bootstrap = script("bootstrap")
restore = script("restore")
secrets_script = script("secrets")


@pytest.mark.parametrize(
    "origin",
    [
        "https://abrl.example.com",
        "http://research.company.io",
        "https://research.company.io:8443",
        "https://research.company.io/lab",
        "https://user@research.company.io",
        "https://other.company.io",
    ],
)
def test_controlled_fixture_origin_must_match_cloud_dns_tls_configuration(origin):
    config = {"domain": "research.company.io"}
    env = {"ABRL_WEB_ORIGIN": "https://research.company.io", "ABRL_CONTROLLED_ORIGIN": origin}
    assert preflight.validate_runtime_origins(config, env)
    env["ABRL_CONTROLLED_ORIGIN"] = "https://research.company.io/"
    assert preflight.validate_runtime_origins(config, env) == []
    assert not preflight.configured_domain("abrl.example.com")


@pytest.mark.parametrize("size", [0, 1, 1024, 8 * 1024 * 1024 + 17])
def test_encrypted_backup_roundtrip(size):
    source = b"a" * size
    encrypted = io.BytesIO()
    key = b"k" * 32
    meta = backup.encrypt_stream(io.BytesIO(source), encrypted, key)
    decrypted = io.BytesIO()
    result = backup.decrypt_stream(io.BytesIO(encrypted.getvalue()), decrypted, key)
    assert decrypted.getvalue() == source
    assert result["sha256"] == meta["sha256"]
    assert result["bytes"] == size


@pytest.mark.parametrize("mutation", ["truncated", "modified", "wrong_key", "trailing"])
def test_backup_refuses_corruption(mutation):
    stream = io.BytesIO()
    key = b"x" * 32
    backup.encrypt_stream(io.BytesIO(b"private profile state"), stream, key)
    encoded = stream.getvalue()
    if mutation == "truncated":
        encoded = encoded[:24]  # No authenticated terminal frame.
    elif mutation == "modified":
        data = bytearray(encoded)
        data[45] ^= 1
        encoded = bytes(data)
    elif mutation == "wrong_key":
        key = b"z" * 32
    else:
        encoded += b"ignored data"
    with pytest.raises(Exception):
        backup.decrypt_stream(io.BytesIO(encoded), io.BytesIO(), key)


def test_capacity_is_hypothesis_and_missing_prices_are_unknown():
    config = json.loads((ROOT / "deployment/cloud.example.json").read_text())
    report = preflight.estimate(config)
    vendor = next(p for p in report["pools"] if p["pool"] == "vendor")
    assert vendor["session_vcpus_at_maximum"] == 2000
    assert vendor["session_memory_gib_at_maximum"] == 4000
    assert vendor["measured"] is False
    assert vendor["maximum_hourly_compute_cost"] is None
    assert all(row["can_fit_all_sessions"] for row in report["pools"])
    assert report["network_attachments_at_ceiling"] < 100
    config["pools"]["vendor"]["max_nodes"] = 80
    assert any("ceiling" in error for error in preflight.validate_config(config))


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": code}}


class LockStore:
    def __init__(self, reject=True, versioned=False):
        self.objects = {}
        self.reject = reject
        self.versioned = versioned

    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled"} if self.versioned else {}

    def put_object(self, **kwargs):
        if kwargs["Key"] in self.objects and self.reject:
            raise S3Error(412)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        if "SSECustomerKey" not in kwargs:
            raise S3Error(400)
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]]), "SSECustomerAlgorithm": "AES256"}

    def delete_object(self, **kwargs):
        del self.objects[kwargs["Key"]]


def test_state_lock_requires_conditional_create_and_encryption():
    store = LockStore()
    preflight.check_state(store, "state", b"k" * 32)
    assert not store.objects
    bad = LockStore(reject=False)
    with pytest.raises(ValueError, match="competing"):
        preflight.check_state(bad, "state", b"k" * 32)
    assert not bad.objects
    with pytest.raises(ValueError, match="never-versioned"):
        preflight.check_state(LockStore(versioned=True), "state", b"k" * 32)


def test_bootstrap_configuration_encrypts_secrets_and_separates_roles():
    control = {"role": "control", "address": "203.0.113.10", "private_address": "10.20.0.10"}
    config = bootstrap.k3s_config(control, [control], "private-join-token", first=True)
    assert config["secrets-encryption"] is True
    assert config["cluster-init"] is True
    assert config["flannel-backend"] == "wireguard-native"
    user_data = bootstrap.worker_cloud_init("vendor", [control], "private-join-token")
    document = json.loads(user_data.removeprefix("#cloud-config\n"))
    shell = base64.b64decode(
        next(f for f in document["write_files"] if f["path"] == "/root/abrl-join.sh")["content"]
    ).decode()
    assert "sha256sum -c" in shell
    assert any(f["path"].endswith("abrl-chromium.json") for f in document["write_files"])
    assert "K3S_NODE_IP" in shell
    assert "private-join-token" not in shell  # Token itself is JSON/base64 encoded.


def test_restore_cannot_overwrite_source_cluster_and_requires_sse():
    with pytest.raises(ValueError, match="new"):
        restore.restore_manifest("abrl-postgres", "https://example.com", "s3://backups/", "key")
    manifest = restore.restore_manifest("abrl-restored", "https://example.com", "s3://backups/", "key")
    source, cluster = manifest["items"]
    assert source["spec"]["configuration"]["s3Credentials"]["sseCustomerKey"]["name"] == "key"
    assert cluster["spec"]["bootstrap"]["recovery"]["source"] == "recovery-source"
    assert "plugins" not in cluster["spec"]  # Do not overwrite the archive being restored.


def render(*values):
    if not shutil.which("helm"):
        pytest.skip("Helm is required for chart contract validation")
    return subprocess.run(
        ["helm", "template", "abrl", str(ROOT / "deployment/chart"), *values],
        capture_output=True,
        text=True,
    )


def test_chart_limits_and_cloud_isolation_gate():
    default = render()
    assert default.returncode == 0
    assert "kind: ScaledJob" not in default.stdout
    denied = render("--set", "cloudWorkers.enabled=true")
    assert denied.returncode != 0 and "isolationAttestation" in denied.stderr
    denied = render("--set", "maximumSessions=501")
    assert denied.returncode != 0
    denied = render(
        "--set",
        "autoscaler.enabled=true",
        "--set",
        "autoscaler.networkId=123",
        "--set",
        "autoscaler.vendor.max=80",
    )
    assert denied.returncode != 0 and "80-node" in denied.stderr


def test_rendered_job_and_database_contracts():
    result = render(
        "--set",
        "cloudWorkers.enabled=true",
        "--set",
        "cloudWorkers.isolationAttestation=test-only",
        "--set",
        "cloudWorkers.vendorEnabled=true",
        "--set",
        "postgres.backupsEnabled=true",
        "--set",
        "postgres.backupEndpoint=https://example.com",
        "--set",
        "postgres.backupDestination=s3://backups/",
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert text.count("kind: ScaledJob") == 2
    assert "browser_sessions" in text and "admitted = true" in text
    assert "strategy: accurate" in text and "strategy: gradual" in text
    assert 'safe-to-evict: "false"' in text and "backoffLimit: 0" in text
    assert "dataDurability: required" in text and "sseCustomerKey:" in text
    assert "ABRL_DEPLOYMENT, value: cloud" in text
    assert "abrl.session_runner, --once" in text
    assert "browser-egress-only-broker" in text
    assert "passwordSecret: {name: abrl-postgres-scaler}" in text
    assert "/app/scripts/cloud-migrate.py" in text


def test_runtime_secret_excludes_recovery_and_scaler_credentials():
    data = {
        "ABRL_DATABASE_URL": "postgresql+psycopg://application",
        "ABRL_BACKUP_SSE_KEY": "recovery-key",
        "ABRL_SCALER_PASSWORD": "scaler-key",
        "ABRL_SCALER_DATABASE_URL": "scaler-dsn",
        "ABRL_ADSCORE_RESPONSE_KEY": "verification-key",
        "ABRL_POSTGRES_PASSWORD": "duplicate-app-key",
        "CLOAKBROWSER_LICENSE_KEY": "license-key",
        "HCLOUD_TOKEN": "cloud-key",
    }
    assert secrets_script.runtime_values(data) == {"ABRL_DATABASE_URL": "postgresql+psycopg://application"}
    with pytest.raises(ValueError, match="Missing required"):
        secrets_script.validate({"ABRL_DEPLOYMENT": "cloud"})


def test_chart_accepts_immutable_images_and_rejects_invalid_digests():
    digest = "sha256:" + "a" * 64
    result = render("--set", "image.digest=" + digest)
    assert result.returncode == 0, result.stderr
    assert "image: abrl@" + digest in result.stdout
    assert render("--set", "image.digest=mutable").returncode != 0


@pytest.mark.asyncio
async def test_cloak_cloud_reservations_and_selected_credentials(monkeypatch):
    from abrl.cloud_runtime import CloudRuntime

    monkeypatch.setenv("ABRL_POD_NAME", "runner-pod")
    fake = KubeFake()
    runtime = CloudRuntime("cloak-session", 1, client=fake, image="cloak:test")
    async with runtime.launch({"adapter": "cloakbrowser", "origin": "https://owned.example"}):
        pods = [v for v in fake.objects.values() if v["kind"] == "Pod"]
        browser = next(p for p in pods if p["metadata"]["labels"]["abrl.io/role"] == "browser")
        container = browser["spec"]["containers"][0]
        assert container["resources"]["requests"]["cpu"] == "4"
        assert container["resources"]["limits"]["cpu"] == "4"
        assert "CLOAKBROWSER_LICENSE_KEY" in json.dumps(container)
        assert "KAMELEO_PAT" not in json.dumps(container)


def test_terraform_fixed_nodes_do_not_own_autoscaled_instances():
    text = (ROOT / "infrastructure/terraform/main.tf").read_text()
    assert "range(3)" in text and "range(2)" in text
    assert 'resource "hcloud_server" "fixed"' in text
    assert text.count('resource "hcloud_server"') == 1
    assert "user_data =" not in text and "token =" not in text
    assert "prevent_destroy = true" in text


class KubeFake:
    def __init__(self, *, duplicate=False, deny_delete=False):
        self.objects = {}
        self.calls = []
        self.duplicate = duplicate
        self.deny_delete = deny_delete

    async def request(self, method, path, json=None):
        import httpx

        self.calls.append((method, path, json))
        if method == "GET" and path.endswith("/pods/runner-pod"):
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "runner-pod",
                        "labels": {"abrl.io/role": "runner"},
                        "ownerReferences": [
                            {"apiVersion": "batch/v1", "kind": "Job", "name": "job-one", "uid": "job-uid"}
                        ],
                    }
                },
            )
        if method == "POST":
            if self.duplicate:
                return httpx.Response(409, json={"message": "existing owner"})
            resource = {**json, "metadata": {**json["metadata"], "uid": str(len(self.objects) + 1)}}
            if json["kind"] == "Service":
                resource["spec"] = {**resource["spec"], "clusterIP": "10.43.10.10"}
            if json["kind"] == "Pod":
                resource["status"] = {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": c["name"], "image": c["image"], "imageID": "sha256:actual", "ready": True}
                        for c in resource["spec"]["containers"]
                    ],
                }
            self.objects[path + "/" + resource["metadata"]["name"]] = resource
            return httpx.Response(201, json=resource)
        if method == "DELETE":
            if self.deny_delete:
                return httpx.Response(403, json={"message": "forbidden"})
            item = self.objects.get(path)
            if item is not None:
                assert json["preconditions"]["uid"] == item["metadata"]["uid"]
                del self.objects[path]
            return httpx.Response(200, json={})
        if path in self.objects:
            return httpx.Response(200, json=self.objects[path])
        return httpx.Response(404, json={})


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence", ["fresh", "persistent"])
async def test_remote_runtime_separates_credentials_and_confirms_cleanup(monkeypatch, persistence):
    from abrl.cloud_runtime import CloudRuntime

    monkeypatch.setenv("ABRL_POD_NAME", "runner-pod")
    fake = KubeFake()
    runtime = CloudRuntime("session-abc", 2, client=fake, image="abrl-browser:test")
    async with runtime.launch(
        {
            "origin": "https://owned.example",
            "persistence": persistence,
            "resource_origins": [],
            "proxy": {"username_ref": "OXY_USER", "password_ref": "OXY_PASS"},
        }
    ) as live:
        assert live.connection["epoch"] == 2
        assert live.token not in json.dumps(live.metadata)
        assert live.token not in repr(live)
        assert live.metadata["browser"]["containers"][0]["image_id"] == "sha256:actual"
        pods = [v for v in fake.objects.values() if v["kind"] == "Pod"]
        assert len(pods) == 2
        browser = next(p for p in pods if p["metadata"]["labels"]["abrl.io/role"] == "browser")
        broker = next(p for p in pods if p["metadata"]["labels"]["abrl.io/role"] == "broker")
        assert browser["spec"]["automountServiceAccountToken"] is False
        assert "OXY_PASS" not in json.dumps(browser)
        assert "abrl-runtime" not in json.dumps(pods)
        assert live.token not in json.dumps(list(fake.objects.values()))
        assert "browser-token" in json.dumps(browser) and "broker-token" not in json.dumps(browser)
        assert "broker-token" in json.dumps(broker) and "browser-token" not in json.dumps(broker)
        assert "abrl-proxy-providers" in json.dumps(broker)
        broker_env = {v["name"]: v.get("value") for v in broker["spec"]["containers"][0]["env"]}
        assert broker_env["ABRL_REMOTE_LIFETIME_SECONDS"] == "960"
        assert all("ownerReferences" in obj["metadata"] for obj in fake.objects.values())
        policies = [v for v in fake.objects.values() if v["kind"] == "NetworkPolicy"]
        assert all(runtime.prefix in json.dumps(p) for p in policies)
        assert all("job-one" in json.dumps(p) for p in policies)
    assert live.stopped is True
    assert not fake.objects


@pytest.mark.asyncio
async def test_remote_runtime_never_adopts_conflicting_owner(monkeypatch):
    from abrl.cloud_runtime import CloudRuntime

    monkeypatch.setenv("ABRL_POD_NAME", "runner-pod")
    fake = KubeFake(duplicate=True)
    runtime = CloudRuntime("session-abc", 2, client=fake, image="abrl-browser:test")
    with pytest.raises(RuntimeError, match="409"):
        async with runtime.launch({"origin": "https://owned.example"}):
            pytest.fail("An existing runtime must never be adopted by a new owner")
    assert not [call for call in fake.calls if call[0] == "DELETE"]


@pytest.mark.asyncio
async def test_remote_runtime_refuses_unsupported_adapters(monkeypatch):
    from abrl.cloud_runtime import CloudRuntime

    fake = KubeFake()
    runtime = CloudRuntime("session-abc", 2, client=fake, image="abrl-browser:test")
    with pytest.raises(ValueError, match="does not support"):
        async with runtime.launch({"adapter": "unsupported_browser"}):
            pytest.fail("Unsupported adapters cannot be substituted by remote launch")
    assert not fake.calls


@pytest.mark.asyncio
async def test_manual_chrome_uses_headed_standard_pool_image(monkeypatch):
    from abrl.cloud_runtime import CloudRuntime

    monkeypatch.setenv("ABRL_POD_NAME", "runner-pod")
    monkeypatch.setenv("ABRL_BROWSER_IMAGE_MANUAL_CHROME", "manual-chrome:test")
    fake = KubeFake()
    runtime = CloudRuntime("manual-session", 1, client=fake)
    async with runtime.launch({"adapter": "manual_chrome", "origin": "https://owned.example"}):
        browser = next(
            p
            for p in fake.objects.values()
            if p["kind"] == "Pod" and p["metadata"]["labels"]["abrl.io/role"] == "browser"
        )
        container = browser["spec"]["containers"][0]
        assert browser["spec"]["nodeSelector"]["abrl.io/pool"] == "standard"
        assert container["image"] == "manual-chrome:test"
        assert "browser-entrypoint.sh" in container["command"][0]
        assert container["resources"]["requests"] == {"cpu": "2", "memory": "4Gi"}
        assert any(e.get("value") == "manual_chrome" for e in container["env"])


class NativeS3:
    def __init__(self, *, source=None, versioning="Enabled", corrupt=False):
        self.objects = source or {}
        self.versioning = versioning
        self.metadata = {}
        self.corrupt = corrupt
        self.uploads = 0

    def get_bucket_versioning(self, **kwargs):
        return {"Status": self.versioning}

    def get_paginator(self, operation):
        return self

    def paginate(self, **kwargs):
        return [
            {
                "Contents": [
                    {"Key": key, "ETag": "etag-" + key, "Size": len(value)}
                    for key, value in self.objects.items()
                ]
            }
        ]

    def head_object(self, **kwargs):
        assert kwargs.get("SSECustomerAlgorithm") == "AES256"
        if kwargs["Key"] not in self.objects:
            raise S3Error(404)
        return {"Metadata": self.metadata.get(kwargs["Key"], {})}

    def get_object(self, **kwargs):
        assert kwargs.get("SSECustomerAlgorithm") == "AES256"
        if kwargs["Key"] not in self.objects:
            raise S3Error(404)
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]] + (b"corrupt" if self.corrupt else b""))}

    def put_object(self, **kwargs):
        assert kwargs.get("SSECustomerAlgorithm") == "AES256"
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def upload_fileobj(self, stream, bucket, key, ExtraArgs):
        assert ExtraArgs["SSECustomerAlgorithm"] == "AES256"
        self.objects[key] = stream.read()
        self.metadata[key] = ExtraArgs["Metadata"]
        self.uploads += 1


def test_native_barman_replication_sse_readback_and_idempotency():
    source = NativeS3(source={"backups/abrl-postgres/wals/001": b"wal bytes"})
    target = NativeS3()
    sse = {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": b"k" * 32}
    assert backup.mirror_native_sse(source, target, "live", "dr", sse, sse) == 1
    assert backup.mirror_native_sse(source, target, "live", "dr", sse, sse) == 0
    assert target.uploads == 1
    assert (
        target.objects["backups/abrl-postgres/wals/001"] == source.objects["backups/abrl-postgres/wals/001"]
    )
    with pytest.raises(ValueError, match="versioning"):
        backup.mirror_native_sse(source, NativeS3(versioning="Suspended"), "live", "dr", sse, sse)
    with pytest.raises(ValueError, match="checksum"):
        backup.mirror_native_sse(source, NativeS3(corrupt=True), "live", "dr", sse, sse)


@pytest.mark.asyncio
async def test_kameleo_sidecar_credentials_and_shared_ownership(monkeypatch):
    from abrl.cloud_runtime import CloudRuntime

    monkeypatch.setenv("ABRL_POD_NAME", "runner-pod")
    monkeypatch.setenv("ABRL_KAMELEO_ENGINE_IMAGE", "kameleo/kameleo-app@sha256:" + "a" * 64)
    monkeypatch.setenv("ABRL_KAMELEO_CONTROL_ORIGINS_JSON", '["https://license.example"]')
    fake = KubeFake()
    runtime = CloudRuntime("vendor-session", 1, client=fake, image="sdk:test")
    async with runtime.launch(
        {"adapter": "kameleo", "origin": "https://owned.example", "persistence": "persistent"}
    ):
        browser = next(
            p
            for p in fake.objects.values()
            if p["kind"] == "Pod" and p["metadata"]["labels"]["abrl.io/role"] == "browser"
        )
        service, engine = browser["spec"]["containers"]
        assert browser["spec"]["securityContext"]["runAsUser"] == 1001
        assert browser["spec"]["nodeSelector"]["abrl.io/pool"] == "vendor"
        assert "KAMELEO_PAT" in json.dumps(engine) and "KAMELEO_PAT" not in json.dumps(service)
        assert {v["mountPath"] for v in engine["volumeMounts"]} == {"/tmp", "/data", "/dev/shm"}
        assert any(e["name"] == "HTTP_PROXY" for e in engine["env"])
        assert engine["image"].endswith("@sha256:" + "a" * 64)
        assert "command" not in engine  # Preserve the vendor's supported engine entrypoint.


def test_seccomp_keeps_sandbox_with_dropped_outer_capabilities():
    profile = json.loads((ROOT / "infrastructure/docker/chromium-seccomp.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {
        name
        for rule in profile["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("includes")
        for name in rule["names"]
    }
    assert {"clone", "setns", "unshare", "chroot"} <= allowed
