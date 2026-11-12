import asyncio
import base64
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from abrl import adapters, network
from abrl.browser import preparation_plan
from abrl.profile_storage import ProfileLock, ProfileStorageError, ProfileStore
from abrl.proxy import (
    OxylabsBinding,
    ProxyConfigurationError,
    classify_proxy_failure,
    select_proxy,
)


def candidate(name="fixed", **extra):
    return {
        "id": name,
        "product": "dedicated_isp",
        "host": "disp.oxylabs.io",
        "port": 8001,
        "region": "IL",
        "exclusive": True,
        "max_connections": 5,
        "active_connections": 0,
        "remaining_minutes": 240,
        "transport_success_rate": 0.99,
        "transport_samples": 100,
        "latency_ms": 25,
        **extra,
    }


def test_proxy_selection_preserves_continuity_and_does_not_use_verdicts():
    existing = candidate("existing", latency_ms=70, adscore_clean_rate=0)
    alternative = candidate("alternative", latency_ms=1, adscore_clean_rate=1)
    result = select_proxy({"region": "IL", "continuity": "fixed"}, [alternative, existing], existing)
    assert result["candidate"]["id"] == "existing"
    assert "adscore_clean_rate" not in result["evidence"]
    assert "continuity" in result["rationale"][0]


def test_proxy_constraints_explain_all_disqualifications():
    decision = select_proxy(
        {"region": "IL", "continuity": "exclusive", "duration_minutes": 60},
        [
            candidate("wrong-country", region="US"),
            candidate("full", active_connections=5),
            candidate("short", remaining_minutes=10),
            candidate("mobile-exit", product="residential", exclusive=False),
        ],
    )
    assert decision["status"] == "unavailable"
    reasons = {entry["id"]: entry["reasons"] for entry in decision["rejected"]}
    assert "geography_not_permitted" in reasons["wrong-country"]
    assert "capacity_exhausted" in reasons["full"]
    assert "insufficient_confirmed_duration" in reasons["short"]
    assert "fixed_exit_required" in reasons["mobile-exit"]


def test_proxy_unknown_health_and_cost_are_not_fabricated():
    decision = select_proxy({}, [candidate(transport_success_rate=None, latency_ms=None)])
    assert decision["status"] == "selected"
    assert decision["evidence"]["cost_per_gb"] is None
    assert decision["evidence"]["transport_success_rate"] is None
    assert (
        select_proxy({"min_transport_success_rate": 0.95}, [candidate(transport_success_rate=None)])[
            "status"
        ]
        == "unavailable"
    )


def test_proxy_fallback_is_only_explicit_and_geography_preserved():
    assert select_proxy({"allow_direct": True}, [])["status"] == "direct"
    assert select_proxy({"allow_direct": True, "region": "IL"}, [])["status"] == "unavailable"
    assert (
        select_proxy({"region": "IL", "fallback_regions": ["DE"]}, [candidate(region="DE")])["status"]
        == "selected"
    )


def test_upstream_recovery_classification_never_replays_ambiguous_action():
    assert classify_proxy_failure(407)["action"] == "pause_pool"
    assert classify_proxy_failure(502, "quota exhausted")["action"] == "pause_pool"
    assert classify_proxy_failure(502)["retryable"]
    assert classify_proxy_failure(502, action_started=True)["action"] == "needs_reconciliation"
    assert not classify_proxy_failure(403)["retryable"]


def binding_config():
    return {
        "host": "pr.oxylabs.io",
        "port": 7777,
        "product": "residential",
        "country": "IL",
        "session_key": "research42",
        "strict_affinity": True,
        "ttl_minutes": 30,
        "username_ref": "PROXY_USER",
        "password_ref": "PROXY_PASSWORD",
        "provider_resolved_ack": True,
    }


def test_proxy_credentials_only_resolve_in_binding_and_never_repr(monkeypatch):
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-example")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "super-private")
    binding = OxylabsBinding.from_config(binding_config())
    decoded = base64.b64decode(binding.authorization().split()[1]).decode()
    assert decoded == "customer-example-cc-IL-sessid_oneip-research42-sesstime-30:super-private"
    assert "super-private" not in repr(binding)
    assert "customer-example" not in str(binding.public_metadata())
    with pytest.raises(ProxyConfigurationError, match="inline"):
        OxylabsBinding.from_config({**binding_config(), "password": "unsafe"})
    with pytest.raises(ProxyConfigurationError, match="DNS"):
        OxylabsBinding.from_config({**binding_config(), "provider_resolved_ack": False})


async def test_guard_upstream_connect_is_tls_authenticated_and_classifies_quota(monkeypatch):
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-example")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "super-private")
    binding = OxylabsBinding.from_config(binding_config())
    captured = {}

    class Sink:
        def write(self, data):
            captured["request"] = data

        async def drain(self):
            pass

        def close(self):
            captured["closed"] = True

        async def wait_closed(self):
            pass

    async def resolve(host, port):
        assert host == binding.host
        return ("93.184.216.34",)

    async def connect(ip, port, **kwargs):
        captured.update(kwargs)
        source = asyncio.StreamReader()
        source.feed_data(
            b"HTTP/1.1 407 Proxy Authentication Required\r\nX-Error-Description: quota\r\n\r\n"
        )
        return source, Sink()

    monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
    monkeypatch.setattr(network.asyncio, "open_connection", connect)
    guard = network.EgressGuard("https://site.example", [], lambda _: None, upstream=binding)
    assert await guard._upstream_connect("site.example:443") is None
    assert captured["ssl"].check_hostname
    assert captured["server_hostname"] == "pr.oxylabs.io"
    assert b"CONNECT site.example:443" in captured["request"]
    assert b"Proxy-Authorization: Basic " in captured["request"]
    assert guard.transport_events[0]["action"] == "pause_pool"
    assert "super-private" not in str(guard.transport_events)


def test_profile_lock_excludes_another_owner_and_releases(tmp_path):
    with ProfileLock(tmp_path / "profile"):
        with pytest.raises(ProfileStorageError, match="owner"):
            ProfileLock(tmp_path / "profile").acquire()
    with ProfileLock(tmp_path / "profile"):
        pass


def test_authenticated_immutable_profile_generations(tmp_path):
    key = os.urandom(32)
    source = tmp_path / "source"
    source.mkdir()
    (source / "Cookies").write_text("sensitive persistent browser state")
    store = ProfileStore(tmp_path / "snapshots", key)
    compatibility = {
        "adapter": "playwright",
        "os": "Darwin",
        "architecture": "arm64",
        "browser_major": "149",
    }
    manifest = store.save(
        "profile1", source, clean_shutdown=True, compatibility=compatibility, generation="first"
    )
    ciphertext = (tmp_path / "snapshots/profile1/first.abrl").read_bytes()
    assert b"sensitive persistent" not in ciphertext
    assert manifest["files"] == 1
    restored = tmp_path / "restored"
    store.restore("profile1", "first", restored, compatibility=compatibility)
    assert (restored / "Cookies").read_text() == "sensitive persistent browser state"
    with pytest.raises(ProfileStorageError, match="immutable"):
        store.save("profile1", source, clean_shutdown=True, compatibility=compatibility, generation="first")
    with pytest.raises(ProfileStorageError, match="compatibility"):
        store.restore(
            "profile1", "first", tmp_path / "wrong-os", compatibility={**compatibility, "os": "Linux"}
        )
    with pytest.raises(ProfileStorageError, match="shutdown"):
        store.save("profile1", source, clean_shutdown=False, compatibility=compatibility)
    corrupted = bytearray(ciphertext)
    corrupted[-1] ^= 1
    (tmp_path / "snapshots/profile1/first.abrl").write_bytes(corrupted)
    with pytest.raises(ProfileStorageError, match="authentication"):
        store.restore("profile1", "first", tmp_path / "corrupted", compatibility=compatibility)


def test_profile_snapshot_rejects_symlinks_and_wrong_key(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "outside").symlink_to(tmp_path / "private")
    store = ProfileStore(tmp_path / "store", os.urandom(32))
    with pytest.raises(ProfileStorageError, match="symbolic"):
        store.save("profile", source, clean_shutdown=True, compatibility={})
    with pytest.raises(ProfileStorageError, match="32 bytes"):
        ProfileStore(tmp_path, b"bad-key")


def test_preparation_plan_is_reproducible_and_cannot_include_transactions():
    config = {"seed": 23, "min_seconds": 30, "max_seconds": 90, "paths": ["/landing", "/help"]}
    plan = preparation_plan(config)
    assert plan == preparation_plan(config)
    assert 30 <= plan["duration_seconds"] <= 90
    assert all(item["action"] in {"navigate", "scroll"} for item in plan["actions"])
    assert len(plan["actions"]) <= 300
    with pytest.raises(ValueError):
        preparation_plan({**config, "paths": ["https://outside.example"]})


def test_remote_browser_cannot_silently_bypass_local_guard(monkeypatch):
    monkeypatch.setenv("ABRL_BROWSER_WS_ENDPOINT", "ws://browser:3000")
    capabilities = adapters.adapter_capabilities()
    assert all(not item["available"] for item in capabilities)
    assert all(item["status"] == "unsupported_operation" for item in capabilities)


async def test_cloak_documented_persistent_contract_and_license_capacity(monkeypatch, tmp_path):
    calls = []

    class Context:
        async def close(self):
            calls.append("close")

    async def persistent(directory, **kwargs):
        calls.append((directory, kwargs))
        return Context()

    package = SimpleNamespace(
        validate_license=lambda _: SimpleNamespace(valid=True, plan="pro"),
        launch_persistent_context_async=persistent,
    )
    seats = SimpleNamespace(active=0, limit=2, state="ok")
    licensing = SimpleNamespace(get_session_seats=lambda _: seats)
    original = adapters.importlib.import_module
    monkeypatch.setattr(
        adapters.importlib,
        "import_module",
        lambda name: (
            package
            if name == "cloakbrowser"
            else licensing
            if name == "cloakbrowser.license"
            else original(name)
        ),
    )
    monkeypatch.setattr(
        adapters, "adapter_capabilities", lambda: [{"id": "cloakbrowser", "available": True}]
    )
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "test-license-never-sent")
    metadata = {}
    async with adapters.launch_adapter(
        "cloakbrowser", "http://127.0.0.1:3001", str(tmp_path / "profile"), metadata
    ):
        assert not metadata["profile_saved"]
    _, arguments = calls[0]
    assert arguments["humanize"] is False and arguments["geoip"] is False
    assert arguments["proxy"] == {"server": "http://127.0.0.1:3001"}
    assert not any("fingerprint" in value for value in arguments["args"])
    assert metadata["profile_saved"] and metadata["runtime_stopped"]
    seats.active = 2
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("cloakbrowser", "http://127.0.0.1:3001", None, {}):
            pytest.fail("Exhausted licensed capacity launched")
    assert failure.value.status == "capacity_exhausted"


@pytest.fixture
def kameleo_contract(monkeypatch):
    events = []
    version = {"engine": "5.4.0"}

    class ProfileAPI:
        def create_profile(self, request, **kwargs):
            events.append(("create", request))
            return SimpleNamespace(id="vendor-profile")

        def import_profile(self, import_profile_request, **kwargs):
            events.append(("import", import_profile_request.path))
            return SimpleNamespace(id="vendor-profile")

        def start_profile(self, identifier, request, **kwargs):
            events.append(("start", request))

        def stop_profile(self, identifier, **kwargs):
            events.append(("stop", identifier))

        def delete_profile(self, identifier, **kwargs):
            events.append(("delete", identifier))

        def export_profile(self, identifier, export_profile_request, **kwargs):
            events.append(("export", identifier))
            Path(export_profile_request.path).write_bytes(b"vendor-profile-archive")

    client = SimpleNamespace(
        profile=ProfileAPI(),
        general=SimpleNamespace(
            healthcheck=lambda **_: None,
            get_user_info=lambda **_: SimpleNamespace(
                version=version["engine"], running_profiles=SimpleNamespace(model_dump=lambda: {})
            ),
        ),
        fingerprint=SimpleNamespace(search_fingerprints=lambda **_: [SimpleNamespace(id="vendor-default")]),
    )
    module = ModuleType("kameleo.local_api_client")
    module.KameleoLocalApiClient = lambda **_: client
    models = ModuleType("kameleo.local_api_client.models")
    for name in ("BrowserSettings", "CreateProfileRequest", "ExportProfileRequest", "ImportProfileRequest"):
        setattr(models, name, lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, "kameleo", ModuleType("kameleo"))
    monkeypatch.setitem(sys.modules, "kameleo.local_api_client", module)
    monkeypatch.setitem(sys.modules, "kameleo.local_api_client.models", models)

    class CDPSession:
        def __init__(self):
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

        async def send(self, command, payload):
            events.append((command, payload))

    class Context:
        pages = [object()]

        def on(self, event, callback):
            pass

        async def new_cdp_session(self, page):
            session = CDPSession()
            events.append(("auth_session", session))
            return session

    class Driver:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        @property
        def chromium(self):
            return self

        async def connect_over_cdp(self, endpoint, **kwargs):
            events.append(("connect", endpoint))
            return SimpleNamespace(contexts=[Context()], close=self.close)

        async def close(self):
            events.append(("disconnect", None))

    monkeypatch.setattr(adapters, "async_playwright", Driver)
    monkeypatch.setattr(adapters, "adapter_capabilities", lambda: [{"id": "kameleo", "available": True}])
    monkeypatch.setattr(adapters, "package_version", lambda _: "5.4.0")
    monkeypatch.setenv("ABRL_KAMELEO_ENDPOINT", "http://127.0.0.1:5050")
    return events, version


async def test_kameleo_stop_export_delete_and_immutable_engine_compatibility(kameleo_contract, tmp_path):
    events, version = kameleo_contract
    metadata = {}
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", str(tmp_path / "profile"), metadata
    ):
        assert metadata["engine_version"] == "5.4.0"
        assert not metadata["profile_saved"]
    names = [name for name, _ in events]
    assert names.index("stop") < names.index("export") < names.index("delete")
    assert metadata["runtime_stopped"] and metadata["profile_saved"]
    assert (tmp_path / "profile/profile.kameleo").read_bytes() == b"vendor-profile-archive"
    assert (
        next(value for name, value in events if name == "connect")
        == "ws://127.0.0.1:5050/playwright/vendor-profile"
    )
    request = next(value for name, value in events if name == "create")
    assert request.fingerprint_id == "vendor-default"
    assert request.start_page == "about:blank"
    events.clear()
    async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3002", str(tmp_path / "profile"), {}):
        pass
    assert events[0][0] == "import"
    version["engine"] = "5.5.0"
    events.clear()
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", None, {}):
            pytest.fail("Incompatible engine launched")
    assert failure.value.status == "incompatible_version" and not events


async def test_kameleo_proxy_capability_never_answers_origin_authentication(kameleo_contract):
    events, _ = kameleo_contract
    secret = {"username": "abrl-session", "password": "ephemeral-proxy-capability"}
    async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", None, {}, proxy_auth=secret):
        cdp = next(value for name, value in events if name == "auth_session")
        callback = cdp.handlers["Fetch.authRequired"]
        await callback({"requestId": "site", "authChallenge": {"source": "Server"}})
        await callback({"requestId": "broker", "authChallenge": {"source": "Proxy"}})
    replies = {
        value["requestId"]: value["authChallengeResponse"]
        for name, value in events
        if name == "Fetch.continueWithAuth"
    }
    assert replies["site"] == {"response": "Default"}
    assert replies["broker"] == {"response": "ProvideCredentials", **secret}


def test_cloud_registry_uses_images_but_runtime_checks_real_packages(monkeypatch):
    monkeypatch.setenv("ABRL_DEPLOYMENT", "cloud")
    monkeypatch.setenv("ABRL_CLOAK_BROWSER_IMAGE", "registry/abrl-cloak@sha256:configured")
    monkeypatch.delenv("ABRL_REMOTE_ADAPTER", raising=False)
    monkeypatch.delenv("ABRL_BROWSER_WS_ENDPOINT", raising=False)
    monkeypatch.delenv("ABRL_EGRESS_PROXY_URL", raising=False)
    result = next(r for r in adapters.adapter_capabilities() if r["id"] == "cloakbrowser")
    assert result["status"] == "configured"
    monkeypatch.setenv("ABRL_REMOTE_ADAPTER", "cloakbrowser")
    monkeypatch.setattr(adapters, "package_version", lambda _: None)
    assert (
        next(r for r in adapters.adapter_capabilities() if r["id"] == "cloakbrowser")["status"]
        == "missing_package"
    )


async def test_native_manual_chrome_has_no_automation_launcher_flags(monkeypatch, tmp_path):
    captured = []
    captured_environment = {}
    proxy_auth_calls = []

    class Process:
        returncode = None
        pid = 999999

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn(*arguments, **kwargs):
        captured.extend(arguments)
        captured_environment.update(kwargs.get("env") or {})
        return process

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get(self, _):
            return SimpleNamespace(
                is_success=True, json=lambda: {"webSocketDebuggerUrl": "ws://127.0.0.1/native-chrome"}
            )

    class Browser:
        contexts = [SimpleNamespace()]

        async def close(self):
            pass

        async def new_browser_cdp_session(self):
            return self

        async def send(self, name):
            assert name == "Browser.close"
            process.returncode = 0

    class Driver:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        @property
        def chromium(self):
            return self

        async def connect_over_cdp(self, endpoint):
            return Browser()

    monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adapters.httpx, "AsyncClient", Client)
    monkeypatch.setattr(adapters, "async_playwright", Driver)
    monkeypatch.setattr(adapters, "chrome_path", lambda: "/native/Google Chrome")

    async def authenticate(context, credentials):
        proxy_auth_calls.append(credentials)

    monkeypatch.setattr(adapters, "_proxy_authentication", authenticate)
    metadata = {}
    async with adapters._native_chrome(
        "http://127.0.0.1:3010",
        str(tmp_path),
        metadata,
        proxy_auth={"username": "abrl-session", "password": "ephemeral-capability"},
        child_env={"DISPLAY": ":99"},
    ):
        assert metadata["launch_mode"] == "native_subprocess"
    assert captured[0] == "/native/Google Chrome"
    assert not any(
        "enable-automation" in str(argument) or "headless" in str(argument) for argument in captured
    )
    assert any(str(argument).startswith("--remote-debugging-port=") for argument in captured)
    assert metadata["runtime_stopped"] and metadata["profile_saved"]

    assert captured_environment == {"DISPLAY": ":99"}
    assert proxy_auth_calls == [{"username": "abrl-session", "password": "ephemeral-capability"}]
    assert "ephemeral-capability" not in str(captured)
    assert metadata["cdp_endpoint"] == "ws://127.0.0.1/native-chrome"


def test_cloud_manual_registry_checks_display_in_the_runtime_not_the_api(monkeypatch):
    monkeypatch.setenv("ABRL_DEPLOYMENT", "cloud")
    monkeypatch.setenv("ABRL_BROWSER_IMAGE_MANUAL_CHROME", "registry/manual@sha256:configured")
    monkeypatch.delenv("ABRL_REMOTE_ADAPTER", raising=False)
    monkeypatch.setattr(adapters, "chrome_path", lambda: "/installed/google-chrome")
    monkeypatch.setattr(adapters.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    def manual():
        return next(row for row in adapters.adapter_capabilities() if row["id"] == "manual_chrome")

    assert manual()["available"] and "human_control" in manual()["capabilities"]
    monkeypatch.setenv("ABRL_REMOTE_ADAPTER", "manual_chrome")
    assert manual()["status"] == "missing_display"
    monkeypatch.setenv("DISPLAY", ":99")
    assert manual()["available"]
