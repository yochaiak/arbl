import asyncio
import base64
import os
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from abrl import adapters, browser, network, profile_storage
from abrl.batch_presets import BatchPresetOptions, select_preset
from abrl.browser import preparation_plan
from abrl.profile_storage import ProfileLock, ProfileStorageError, ProfileStore
from abrl.proxy import (
    DecodoBinding,
    OxylabsBinding,
    ProxyBinding,
    ProxyConfigurationError,
    classify_proxy_failure,
    select_proxy,
)


@pytest.mark.parametrize("provider", ["oxylabs", "decodo"])
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, b'{"ip":"::ffff:8.8.4.4"}', "8.8.4.4"),
        (200, b'{"ip":"2606:4700:4700::1111"}', "2606:4700:4700::1111"),
        (200, b'{"ip":"127.0.0.1"}', None),
        (200, b'{"ip":"ff02::1%eth0"}', None),
        (200, b'{"ip":123}', None),
        (200, b'invalid JSON', None),
        (200, b' ' * 8193, None),
        (302, b'{"ip":"8.8.4.4"}', None),
    ],
)
async def test_proxy_check_accepts_only_a_bounded_public_address(monkeypatch, provider, status, body, expected):
    if provider == "decodo":
        binding = decodo_test_binding(monkeypatch)
        origin, path = "https://ip.decodo.com", "/json"
        if body.startswith(b"{"):
            body = b'{"proxy":' + body + b'}'
    else:
        binding = OxylabsBinding("pr.oxylabs.io", 7777, "residential", "proxy-user", "proxy-password")
        origin, path = "https://ip.oxylabs.io", "/location"
    observed_requests = []
    guard_type = network.EgressGuard
    async_client = httpx.AsyncClient

    def guard(host, resources, deny, *, upstream, **kwargs):
        assert host == origin
        assert resources == []
        assert upstream is binding
        return guard_type(host, resources, deny, upstream=upstream, **kwargs)

    def response(request):
        observed_requests.append((request.method, str(request.url)))
        return httpx.Response(
            status, headers={"Location": "https://other.example"}, stream=httpx.ByteStream(body)
        )

    def client(*, proxy, trust_env, follow_redirects):
        assert proxy.startswith("http://127.0.0.1:")
        assert not trust_env and not follow_redirects
        return async_client(transport=httpx.MockTransport(response), trust_env=False)

    monkeypatch.setattr(network, "EgressGuard", guard)
    monkeypatch.setattr(network.httpx, "AsyncClient", client)
    check = network.check_proxy_ip if provider == "decodo" else network.check_oxylabs_ip
    assert await check(binding) == expected
    assert observed_requests == [("GET", origin + path)]


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


def test_decodo_proxy_selection_is_explicit_and_never_falls_back_to_oxylabs_or_direct():
    oxylabs = candidate("oxylabs", region="US")
    decodo = candidate(
        "decodo", provider="decodo", host="gate.decodo.com", port=7000,
        product="residential", country="US", region="US", exclusive=False,
    )
    assert select_proxy({}, [decodo, oxylabs])["candidate"]["id"] == "oxylabs"
    policy = {"provider": "decodo", "continuity": "sticky"}
    assert select_proxy(policy, [oxylabs, decodo])["candidate"]["id"] == "decodo"
    assert select_proxy(policy, [oxylabs])["status"] == "unavailable"
    assert select_proxy({**policy, "continuity": "strict"}, [decodo])["status"] == "unavailable"
    assert select_proxy({"provider": "decodo", "allow_direct": True}, [oxylabs])["status"] == "unavailable"


def test_upstream_recovery_classification_never_replays_ambiguous_action():
    assert classify_proxy_failure(407)["action"] == "pause_pool"
    assert classify_proxy_failure(502, "quota exhausted")["action"] == "pause_pool"
    assert classify_proxy_failure(502, "upstream load balancer unavailable")["retryable"]
    assert classify_proxy_failure(502, "invalid credentials")["kind"] == "authentication"
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


def decodo_test_binding(monkeypatch):
    monkeypatch.setenv("ABRL_SECRET_DECODO_USER", "testuser01")
    monkeypatch.setenv("ABRL_SECRET_DECODO_PASSWORD", "test+secret")
    binding = ProxyBinding.from_config({
        **binding_config(),
        "provider": "decodo",
        "host": "gate.decodo.com",
        "port": 7000,
        "country": "US",
        "strict_affinity": False,
        "username_ref": "DECODO_USER",
        "password_ref": "DECODO_PASSWORD",
    })
    assert isinstance(binding, DecodoBinding)
    assert binding.provider == "decodo"
    return binding


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
    assert "reason" not in guard.transport_events[0]
    assert "super-private" not in str(guard.transport_events)


@pytest.mark.parametrize("provider", ["oxylabs", "decodo"])
async def test_guard_preserves_sticky_binding_and_redacts_denied_authorities(monkeypatch, provider):
    if provider == "decodo":
        binding = decodo_test_binding(monkeypatch)
        expected_credentials = b"user-testuser01-country-us-session-research42-sessionduration-30:test+secret"
    else:
        monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-example")
        monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "super-private")
        binding = ProxyBinding.from_config(binding_config())
        assert isinstance(binding, OxylabsBinding)
        expected_credentials = b"customer-example-cc-IL-sessid_oneip-research42-sesstime-30:super-private"
    browser_connect = asyncio.open_connection
    outbound = []
    gateway_ip = "8.8.4.4"
    destination_ip = "93.184.216.34"
    hostname = "assets.example:443"
    literal = f"{destination_ip}:443"
    learned = "learned.example:443"

    async def resolve(host, port):
        if host == binding.host:
            assert port == binding.port
            return (gateway_ip,)
        assert host in {"assets.example", "learned.example", destination_ip} and port == 443
        return (destination_ip,)

    async def connect(ip, port, *, family, ssl, server_hostname):
        # There is no direct origin dial or retry through another gateway.
        assert (ip, port, server_hostname) == (gateway_ip, binding.port, binding.host)
        assert ssl.check_hostname
        source = asyncio.StreamReader()

        def send(payload):
            first, host, authorization, *_ = payload.split(b"\r\n")
            method, authority, version = first.decode("ascii").split()
            assert (method, version) == ("CONNECT", "HTTP/1.1")
            assert host == f"Host: {authority}".encode("ascii")
            outbound.append((authority, authorization))
            if authority == hostname:
                source.feed_data(b"HTTP/1.1 200 Connection Established\r\n\r\nopaque target bytes")
            else:
                assert authority == literal
                source.feed_data(
                    b"HTTP/1.1 403 Forbidden\r\nX-Error-Description: private provider detail\r\n\r\n"
                )
            source.feed_eof()

        async def noop():
            pass

        return source, SimpleNamespace(write=send, drain=noop, close=lambda: None, wait_closed=noop)

    monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
    monkeypatch.setattr(network.asyncio, "open_connection", connect)
    denied = []
    async with network.EgressGuard(
        "https://site.example", ["https://assets.example", f"https://{destination_ip}"],
        denied.append, upstream=binding,
    ) as guard:
        assert guard.allow_asset_origin("https://learned.example")
        broker = urlsplit(guard.url)

        async def tunnel(authority):
            reader, writer = await browser_connect(broker.hostname, broker.port)
            try:
                writer.write(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode("ascii"))
                await writer.drain()
                return await asyncio.wait_for(reader.read(), timeout=3)
            finally:
                writer.close()
                await writer.wait_closed()

        assert await tunnel(hostname) == b"HTTP/1.1 200 Connection Established\r\n\r\nopaque target bytes"
        assert (await tunnel(literal)).startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
        assert (await tunnel(learned)).startswith(b"HTTP/1.1 502 Bad Gateway\r\n")
        assert await tunnel(hostname) == b"HTTP/1.1 200 Connection Established\r\n\r\nopaque target bytes"

    credentials = b"Proxy-Authorization: Basic " + base64.b64encode(expected_credentials)
    assert outbound == [(authority, credentials) for authority in (hostname, literal, literal, hostname)]
    assert denied == []
    assert guard.transport_events == [
        {
            **classify_proxy_failure(403),
            "status": 403,
            "reason": "proxy_destination_denied",
        }
        for _ in (literal, learned)
    ]
    assert destination_ip not in str(guard.transport_events)
    assert "private provider detail" not in str(guard.transport_events)


@pytest.mark.parametrize(
    "failure, reason, status, retryable",
    [
        ("gateway_timeout", "proxy_gateway_timeout", None, True),
        ("gateway_dns_transient", "proxy_gateway_dns_unavailable", None, True),
        ("gateway_dns_denied", "proxy_gateway_dns_denied", None, False),
        ("gateway_tls_invalid", "proxy_gateway_tls_failed", None, False),
        ("tunnel_timeout", "proxy_tunnel_timeout", None, True),
        ("tunnel_closed", "proxy_tunnel_failed", None, True),
        ("tunnel_rejected", "proxy_tunnel_failed", 502, True),
        ("tunnel_invalid", "proxy_response_invalid", None, False),
    ],
)
async def test_proxy_diagnostics_distinguish_transport_failures(monkeypatch, failure, reason, status, retryable):
    monkeypatch.setenv("ABRL_SECRET_PROXY_USER", "customer-example")
    monkeypatch.setenv("ABRL_SECRET_PROXY_PASSWORD", "super-private")
    binding = OxylabsBinding.from_config(binding_config())
    clock = 10.0

    async def resolve(host, port):
        nonlocal clock
        clock += 0.05
        if failure == "gateway_dns_transient":
            raise network.TemporaryDNSFailure("private upstream message")
        if failure == "gateway_dns_denied":
            raise network.NetworkDenied("private upstream message")
        return ("93.184.216.34",)

    async def read_response(separator):
        nonlocal clock
        clock += 0.4
        if failure == "tunnel_timeout":
            raise TimeoutError("private upstream message")
        if failure == "tunnel_closed":
            raise asyncio.IncompleteReadError(b"private upstream message", None)
        if failure == "tunnel_invalid":
            return b"not HTTP private upstream message\r\n\r\n"
        return b"HTTP/1.1 502 Bad Gateway\r\nX-Error-Description: private upstream message\r\n\r\n"

    async def noop():
        pass

    async def connect(ip, port, **kwargs):
        nonlocal clock
        clock += 0.25
        if failure == "gateway_timeout":
            raise TimeoutError("private upstream message")
        if failure == "gateway_tls_invalid":
            raise ssl.SSLCertVerificationError("private upstream message")
        return SimpleNamespace(readuntil=read_response), SimpleNamespace(
            write=lambda _: None, drain=noop, close=lambda: None, wait_closed=noop
        )

    monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
    monkeypatch.setattr(network.asyncio, "open_connection", connect)
    monkeypatch.setattr(network, "monotonic", lambda: clock)
    guard = network.EgressGuard("https://site.example", [], lambda _: None, upstream=binding)
    assert await guard._upstream_connect("site.example:443") is None
    [event] = guard.transport_events
    wait = event.pop("tunnel_wait", None)
    assert event == {
        "kind": "transport",
        "action": "replace_at_checkpoint" if retryable else "pause_assignment",
        "retryable": retryable,
        "status": status,
        "reason": reason,
    }
    if failure in {"gateway_timeout", "tunnel_timeout"}:
        stage, elapsed = ("gateway_tls_connect", 250) if failure == "gateway_timeout" else ("proxy_connect", 400)
        assert wait == {"stage": stage, "stage_elapsed_ms": elapsed}
    else:
        assert wait is None


async def test_tunnel_cleanup_aborts_stalled_tls_shutdown():
    calls = []

    async def wait_closed():
        await asyncio.Future()

    sink = SimpleNamespace(
        close=lambda: calls.append("close"),
        wait_closed=wait_closed,
        transport=SimpleNamespace(abort=lambda: calls.append("abort")),
    )

    await asyncio.wait_for(network._close_tunnel(sink), timeout=1)
    assert calls == ["close", "abort"]


def test_profile_lock_excludes_another_owner_and_releases(tmp_path):
    with ProfileLock(tmp_path / "profile"):
        with pytest.raises(ProfileStorageError, match="owner"):
            ProfileLock(tmp_path / "profile").acquire()
    with ProfileLock(tmp_path / "profile"):
        pass


@pytest.mark.parametrize("operation", ["acquire", "release"])
def test_profile_lock_closes_descriptor_on_flock_failure(monkeypatch, tmp_path, operation):
    lock = ProfileLock(tmp_path)
    if operation == "release":
        lock.acquire()
    descriptors = []

    def fail(fd, _):
        descriptors.append(fd)
        raise OSError("simulated lock IO failure")

    monkeypatch.setattr(profile_storage.fcntl, "flock", fail)
    with pytest.raises(OSError, match="simulated lock IO failure"):
        getattr(lock, operation)()

    assert lock.fd is None
    [descriptor] = descriptors
    with pytest.raises(OSError):
        os.fstat(descriptor)


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


@pytest.fixture
def viewport_only_preset():
    return select_preset(
        BatchPresetOptions(presets=["compact_390"]), ordinal=1, seed=42, application="viewport_only",
    )


@pytest.mark.parametrize("persistent", [False, True], ids=["fresh", "persistent"])
@pytest.mark.parametrize("child_env", [None, {"PATH": "/test"}], ids=["local", "broker-runtime"])
async def test_cloak_documented_launch_contract_and_license_capacity(
    monkeypatch, tmp_path, persistent, child_env, viewport_only_preset,
):
    launches = []
    closed = []

    class Context:
        async def close(self):
            closed.append("context")

    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs == {"accept_downloads": False, "service_workers": "block"}
            return Context()

        async def close(self):
            closed.append("browser")

    async def launch_context(**kwargs):
        launches.append((None, kwargs))
        return Context()

    async def launch_persistent_context(directory, **kwargs):
        launches.append((directory, kwargs))
        return Context()

    async def launch_browser(**kwargs):
        launches.append((None, kwargs))
        return Browser()

    package = SimpleNamespace(
        validate_license=lambda _: SimpleNamespace(valid=True, plan="pro"),
        launch_context_async=launch_context,
        launch_persistent_context_async=launch_persistent_context,
        launch_async=launch_browser,
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
    profile_dir = str(tmp_path / "profile") if persistent else None
    metadata = {}
    async with adapters.launch_adapter(
        "cloakbrowser", "http://127.0.0.1:3001", profile_dir, metadata,
        child_env=child_env, batch_preset=viewport_only_preset,
    ):
        assert not metadata["profile_saved"]
    [(launched_directory, arguments)] = launches
    assert launched_directory == profile_dir
    assert arguments.get("env") == child_env
    assert arguments["humanize"] is True and arguments["geoip"] is True
    assert arguments["proxy"] == {"server": "http://127.0.0.1:3001"}
    if persistent or child_env is None:
        assert arguments["accept_downloads"] is False
        assert arguments["service_workers"] == "block"
    assert arguments["args"].count("--license-route=proxy") == 1
    assert arguments["args"].count("--disable-quic") == 1
    assert "--enable-quic" not in arguments["args"]
    assert not any("fingerprint" in value for value in arguments["args"])
    assert not {"viewport", "screen", "device_scale_factor", "locale", "timezone_id", "user_agent"} & arguments.keys()
    assert closed == (["context", "browser"] if child_env is not None and not persistent else ["context"])
    assert metadata["profile_saved"] is persistent and metadata["runtime_stopped"]
    seats.active = 2
    unavailable = {}
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("cloakbrowser", "http://127.0.0.1:3001", None, unavailable):
            pytest.fail("Exhausted licensed capacity launched")
    assert failure.value.status == "capacity_exhausted"
    assert unavailable["runtime_stopped"] and unavailable["browser_closed"]

    if not persistent and child_env is None:
        async def stalled_context(**_kwargs):
            await asyncio.Event().wait()

        seats.active = 0
        package.launch_context_async = stalled_context
        monkeypatch.setattr(adapters, "_LAUNCH_TIMEOUT_MS", 5)
        timeout_metadata = {}
        with pytest.raises(adapters.AdapterUnavailable) as timeout:
            async with adapters.launch_adapter("cloakbrowser", "http://127.0.0.1:3001", None, timeout_metadata):
                pytest.fail("A stalled context launch returned a runtime")
        assert timeout.value.status == "launch_timeout"
        assert timeout_metadata["runtime_stopped"] is False


@pytest.mark.parametrize(
    ("adapter", "resource_origins", "license_admitted", "geoip_admitted"),
    [
        ("cloakbrowser", [], True, True),
        ("cloakbrowser", ["https://cloakbrowser.dev", "https://api.ipify.org"], False, False),
        ("playwright", [], False, False),
    ],
)
async def test_local_vendor_scope_before_forwarding(
    monkeypatch, adapter, resource_origins, license_admitted, geoip_admitted
):
    guards = []
    decisions = []

    class Guard(network.EgressGuard):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            guards.append(self)

    async def emit(payload):
        if payload["kind"] == "launching":
            decisions.extend(
                guards[0].forwarding_allowed(method, address)
                for method, address in [
                    ("CONNECT", "cloakbrowser.dev:443"),
                    ("CONNECT", "other.cloakbrowser.dev:443"),
                    ("GET", "https://cloakbrowser.dev/"),
                    ("CONNECT", "api.ipify.org:443"),
                    ("CONNECT", "site.example:443"),
                ]
            )
            raise RuntimeError("stop before browser launch")

    async def never_cancel():
        return False

    monkeypatch.setattr(browser, "EgressGuard", Guard)
    await browser.execute_workflow(
        origin="https://site.example",
        resource_origins=resource_origins,
        steps=[{"action": "navigate", "url": "/"}],
        cancel=never_cancel,
        adapter=adapter,
        emit=emit,
    )
    assert decisions == [license_admitted, False, False, geoip_admitted, False]


@pytest.fixture
def kameleo_contract(monkeypatch):
    events = []
    engine = {"version": "5.4.0"}

    class ApiException(Exception):
        def __init__(self, error_code):
            self.error_code = error_code

    class ProfileAPI:
        def create_profile(self, request, **kwargs):
            events.append(("create", request))
            return SimpleNamespace(id="vendor-profile")

        def import_profile(self, import_profile_request, **kwargs):
            events.append(("import", import_profile_request.path))
            engine["imported_profile_bytes"] = Path(import_profile_request.path).read_bytes()
            return SimpleNamespace(id="vendor-profile")

        def update_profile(self, identifier, update_profile_request, **kwargs):
            events.append(("update", (identifier, update_profile_request)))
            return SimpleNamespace(id=identifier)

        def start_profile(self, identifier, request, **kwargs):
            events.append(("start", request))
            if engine.get("start_error_code"):
                raise ApiException(engine["start_error_code"])
            if engine.get("fail_start"):
                raise RuntimeError("Vendor startup details")

        def stop_profile(self, identifier, **kwargs):
            events.append(("stop", identifier))
            if engine.get("fail_stop"):
                raise RuntimeError("Vendor shutdown details")

        def get_profile_status(self, identifier, **kwargs):
            events.append(("status", identifier))
            return SimpleNamespace(lifetime_state=engine.get("lifetime_state", "unknown"))

        def delete_profile(self, identifier, **kwargs):
            events.append(("delete", identifier))
            if engine.get("delete_error_code"):
                raise ApiException(engine["delete_error_code"])
            if engine.get("fail_delete"):
                raise RuntimeError("Vendor deletion details")

        def export_profile(self, identifier, export_profile_request, **kwargs):
            events.append(("export", identifier))
            Path(export_profile_request.path).write_bytes(b"vendor-profile-archive")
            if engine.get("export_error_code"):
                raise ApiException(engine["export_error_code"])

    def healthcheck(**_):
        if error := engine.get("health_error"):
            raise error

    client = SimpleNamespace(
        profile=ProfileAPI(),
        general=SimpleNamespace(
            healthcheck=healthcheck,
            get_user_info=lambda **_: SimpleNamespace(
                version=engine["version"], running_profiles=SimpleNamespace(model_dump=lambda: {})
            ),
        ),
        fingerprint=SimpleNamespace(search_fingerprints=lambda **_: [SimpleNamespace(id="vendor-default")]),
    )
    module = ModuleType("kameleo.local_api_client")
    module.KameleoLocalApiClient = lambda **_: client
    exceptions = ModuleType("kameleo.local_api_client.exceptions")
    exceptions.ApiException = ApiException
    models = ModuleType("kameleo.local_api_client.models")
    for name in (
        "BrowserSettings",
        "CreateProfileRequest",
        "ExportProfileRequest",
        "ImportProfileRequest",
        "ProxyChoice",
        "Server",
        "UpdateProfileRequest",
    ):
        setattr(models, name, lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, "kameleo", ModuleType("kameleo"))
    monkeypatch.setitem(sys.modules, "kameleo.local_api_client", module)
    monkeypatch.setitem(sys.modules, "kameleo.local_api_client.exceptions", exceptions)
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
            if error := engine.get("cdp_error"):
                raise error
            context = Context()
            engine["cdp_context"] = context
            return SimpleNamespace(contexts=[context], close=self.close)

        async def close(self):
            events.append(("disconnect", None))

    monkeypatch.setattr(adapters, "async_playwright", Driver)
    monkeypatch.setattr(adapters, "adapter_capabilities", lambda: [{"id": "kameleo", "available": True}])
    monkeypatch.setattr(adapters, "package_version", lambda _: "5.4.0")
    monkeypatch.setenv("ABRL_KAMELEO_ENDPOINT", "http://127.0.0.1:5050")
    return events, engine


async def test_kameleo_unavailable_healthcheck_does_not_touch_persistent_profile(kameleo_contract, tmp_path):
    events, engine = kameleo_contract
    engine["health_error"] = TimeoutError()
    archive = tmp_path / "profile.kameleo"
    archive.write_bytes(b"previous profile")
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", str(tmp_path), metadata):
            pytest.fail("Engine startup proceeded after failed healthcheck")
    assert failure.value.status == "engine_unavailable"
    assert metadata["runtime_stopped"] and not metadata.get("profile_save_failed")
    assert not events and archive.read_bytes() == b"previous profile"


@pytest.mark.parametrize("archive_exists", [False, True], ids=["missing", "empty"])
async def test_kameleo_prevents_empty_fallback_when_restoring(kameleo_contract, tmp_path, archive_exists):
    events, _ = kameleo_contract
    if archive_exists:
        (tmp_path / "profile.kameleo").touch()
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable, match="archive is missing or empty") as failure:
        async with adapters.launch_adapter(
            "kameleo", "http://127.0.0.1:3001", str(tmp_path), metadata,
            expected_compatibility={},
        ):
            pytest.fail("Kameleo launched without the expected profile")
    assert failure.value.status == "profile_restore_failed"
    assert not events
    assert metadata["runtime_stopped"] and "profile_launch_state" not in metadata


async def test_kameleo_cdp_timeout_releases_import_and_preserves_archive(kameleo_contract, tmp_path):
    events, engine = kameleo_contract
    engine["cdp_error"] = adapters.PlaywrightTimeout("private attachment detail")
    archive = tmp_path / "profile.kameleo"
    archive.write_bytes(b"prior profile")
    metadata = {}

    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", str(tmp_path), metadata):
            pytest.fail("Delivered a runtime after CDP attach timed out")

    assert failure.value.status == "launch_timeout"
    assert "private attachment detail" not in str(failure.value)
    assert [name for name, _ in events] == ["import", "update", "start", "connect", "stop", "delete"]
    assert metadata["runtime_stopped"] and not metadata["profile_saved"]
    assert archive.read_bytes() == b"prior profile"
    with ProfileLock(tmp_path):
        pass


async def test_kameleo_preserves_workflow_error_when_teardown_fails(kameleo_contract):
    _, engine = kameleo_contract
    metadata = {}
    failure = RuntimeError("workflow failure")
    with pytest.raises(RuntimeError) as raised:
        async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", None, metadata):
            engine["fail_stop"] = True
            raise failure

    assert raised.value is failure
    assert not metadata["runtime_stopped"] and "cleanup_error" in metadata


async def test_kameleo_license_denial_reports_unsaved_profile_after_stop(kameleo_contract, tmp_path):
    events, engine = kameleo_contract
    engine["export_error_code"] = "no_capability"
    archive = tmp_path / "profile.kameleo"
    archive.write_bytes(b"previous profile")
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable, match="export is unavailable") as failure:
        async with adapters.launch_adapter("kameleo", "http://127.0.0.1:3001", str(tmp_path), metadata):
            pass
    assert failure.value.status == "persistent_export_unavailable"
    assert metadata["runtime_stopped"] and metadata["profile_save_failed"]
    assert not metadata["profile_saved"]
    assert [operation for operation, _ in events if operation in {"stop", "export", "delete"}] == ["stop", "export"]
    assert archive.read_bytes() == b"previous profile" and not (tmp_path / "profile.pending.kameleo").exists()


async def test_kameleo_stop_export_delete_and_immutable_engine_compatibility(kameleo_contract, tmp_path):
    events, engine = kameleo_contract
    metadata = {}
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", str(tmp_path / "profile"), metadata, kameleo_fixture=True
    ):
        assert metadata["engine_version"] == "5.4.0"
        assert not metadata["profile_saved"]
        assert metadata["profile_launch_state"] == "initialized"
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
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3002", str(tmp_path / "profile"), {}, kameleo_fixture=True
    ) as runtime:
        assert runtime.context is engine["cdp_context"]
        assert runtime.metadata["profile_launch_state"] == "restored"
        assert engine["imported_profile_bytes"] == b"vendor-profile-archive"
    assert events[0][0] == "import"
    engine["version"] = "5.5.0"
    events.clear()
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter(
            "kameleo", "http://127.0.0.1:3001", None, {}, kameleo_fixture=True
        ):
            pytest.fail("Incompatible engine launched")
    assert failure.value.status == "incompatible_version" and not events


async def test_kameleo_launch_uses_loopback_broker_without_fixture_flag(kameleo_contract, viewport_only_preset):
    events, _ = kameleo_contract
    metadata = {}
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", None, metadata, batch_preset=viewport_only_preset,
    ):
        request = next(value for name, value in events if name == "create")
        assert request.proxy.value == "http"
        assert (request.proxy.extra.host, request.proxy.extra.port) == ("127.0.0.1", 3001)
    assert metadata["runtime_stopped"] and metadata["browser_closed"]
    assert events[-1] == ("delete", "vendor-profile")


@pytest.mark.parametrize(("lifetime_state", "stopped"), [("created", True), ("starting", False)])
async def test_kameleo_failed_launch_checks_profile_state_before_cleanup(
    kameleo_contract, lifetime_state, stopped
):
    events, engine = kameleo_contract
    engine.update(fail_start=True, fail_stop=True, lifetime_state=lifetime_state)
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable, match="Kameleo lifecycle failed"):
        async with adapters.launch_adapter(
            "kameleo", "http://127.0.0.1:3001", None, metadata, kameleo_fixture=True
        ):
            pytest.fail("Kameleo unexpectedly started")
    expected_calls = ["create", "start", "stop", "status"]
    if stopped:
        expected_calls.append("delete")
    assert [name for name, _ in events] == expected_calls
    assert metadata["runtime_stopped"] is stopped and metadata["browser_closed"] is stopped
    assert ("cleanup_error" in metadata) is not stopped


@pytest.mark.parametrize(
    ("start_error_code", "delete_error_code", "stopped"),
    [
        ("validation_failed", None, True),
        (None, None, False),
        ("validation_failed", "profile_running", False),
    ],
)
async def test_kameleo_delete_failure_keeps_only_confirmed_shutdown(
    kameleo_contract, start_error_code, delete_error_code, stopped
):
    events, engine = kameleo_contract
    engine.update(
        fail_start=True,
        start_error_code=start_error_code,
        fail_stop=True,
        lifetime_state="created",
        fail_delete=True,
        delete_error_code=delete_error_code,
    )
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable, match="Kameleo lifecycle failed"):
        async with adapters.launch_adapter(
            "kameleo", "http://127.0.0.1:3001", None, metadata, kameleo_fixture=True
        ):
            pytest.fail("Kameleo unexpectedly started")

    assert [name for name, _ in events] == ["create", "start", "stop", "status", "delete"]
    assert metadata["runtime_stopped"] is stopped and metadata["browser_closed"] is stopped
    assert ("vendor_profile_cleanup_error" in metadata) is stopped
    assert ("cleanup_error" in metadata) is not stopped


async def test_kameleo_delete_failure_preserves_confirmed_export(kameleo_contract, tmp_path):
    events, engine = kameleo_contract
    engine.update(fail_delete=True)
    metadata = {}
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", str(tmp_path / "profile"), metadata, kameleo_fixture=True
    ):
        pass

    assert [name for name, _ in events][-3:] == ["update", "export", "delete"]
    assert metadata["runtime_stopped"] and metadata["browser_closed"] and metadata["profile_saved"]
    assert metadata["vendor_profile_cleanup_error"] == "Kameleo could not delete the stopped profile"
    assert "cleanup_error" not in metadata
    assert (tmp_path / "profile/profile.kameleo").read_bytes() == b"vendor-profile-archive"


async def test_kameleo_fixture_replaces_and_clears_broker_proxy_before_export(kameleo_contract, tmp_path):
    events, _ = kameleo_contract
    profile_dir = str(tmp_path / "profile")
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", profile_dir, {}, kameleo_fixture=True
    ):
        create = next(request for name, request in events if name == "create")
        assert create.start_page == "about:blank"
        assert create.proxy.value == "http"
        assert vars(create.proxy.extra) == {"host": "127.0.0.1", "port": 3001}

        start = next(request for name, request in events if name == "start")
        assert {
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            '--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"',
            "--proxy-bypass-list=<-loopback>;127.0.0.1:5050",
        } <= set(start.arguments)
        assert not any(argument.startswith("--proxy-server") for argument in start.arguments)

    names = [name for name, _ in events]
    assert names.index("stop") < names.index("update") < names.index("export") < names.index("delete")
    [(identifier, cleared)] = [value for name, value in events if name == "update"]
    assert identifier == "vendor-profile" and cleared.proxy is None

    events.clear()
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3002", profile_dir, {}, kameleo_fixture=True
    ):
        names = [name for name, _ in events]
        assert names.index("import") < names.index("update") < names.index("start")
        [(identifier, replaced)] = [value for name, value in events if name == "update"]
        assert identifier == "vendor-profile"
        assert replaced.proxy.value == "http"
        assert vars(replaced.proxy.extra) == {"host": "127.0.0.1", "port": 3002}

    updates = [value for name, value in events if name == "update"]
    assert len(updates) == 2 and updates[1][1].proxy is None
    names = [name for name, _ in events]
    assert names.index("stop") < names.index("update", names.index("stop")) < names.index("export")


async def test_kameleo_fixture_rejects_a_non_loopback_broker(kameleo_contract):
    events, _ = kameleo_contract
    with pytest.raises(adapters.AdapterUnavailable, match="requires a loopback broker"):
        async with adapters.launch_adapter(
            "kameleo", "http://broker.example:3001", None, {}, kameleo_fixture=True
        ):
            pytest.fail("Kameleo fixture accepted a non-loopback broker")
    assert not events


async def test_kameleo_proxy_capability_never_answers_origin_authentication(kameleo_contract):
    events, _ = kameleo_contract
    secret = {"username": "abrl-session", "password": "ephemeral-proxy-capability"}
    async with adapters.launch_adapter(
        "kameleo", "http://127.0.0.1:3001", None, {}, proxy_auth=secret, kameleo_fixture=True
    ):
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


async def test_saved_wrapper_mismatch_rejects_profile_before_browser_launch(monkeypatch, tmp_path):
    def unexpected_browser_launch():
        pytest.fail("Browser launched with an incompatible profile")

    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setattr(adapters, "package_version", lambda _: "2.0")
    monkeypatch.setattr(adapters, "async_playwright", unexpected_browser_launch)
    profile = tmp_path / "restored-profile"
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable, match="compatibility") as exc:
        async with adapters.launch_adapter(
            "playwright", "http://127.0.0.1:3001", str(profile), metadata,
            expected_compatibility={"wrapper_version": "1.0"},
        ):
            pytest.fail("Incompatible profile was opened")

    assert exc.value.status == "profile_incompatible"
    assert metadata["runtime_stopped"] and not metadata["profile_saved"]
    assert metadata["profile_compatible"] is False
    assert not profile.exists()


@pytest.mark.parametrize("close_succeeds", [True, False])
async def test_context_failure_reports_only_verified_browser_shutdown(monkeypatch, close_succeeds):
    async def new_context(**_):
        raise RuntimeError("Context setup failed")

    async def close():
        if not close_succeeds:
            raise RuntimeError("Shutdown failed")

    async def launch(**_):
        return SimpleNamespace(new_context=new_context, close=close)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setattr(adapters, "async_playwright", playwright)
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("playwright", "http://127.0.0.1:3001", None, metadata):
            pytest.fail("Context setup unexpectedly succeeded")

    assert failure.value.status == "launch_failed"
    assert "Context setup failed" not in str(failure.value)
    assert metadata["runtime_stopped"] is close_succeeds
    assert metadata["browser_closed"] is close_succeeds


@pytest.mark.parametrize("adapter", ["playwright", "cloakbrowser"])
@pytest.mark.parametrize("empty_directory", [False, True])
async def test_missing_chromium_restore_state_never_launches(monkeypatch, tmp_path, adapter, empty_directory):
    def unexpected_browser_launch():
        pytest.fail("Browser launched without the saved profile")

    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setattr(adapters, "async_playwright", unexpected_browser_launch)
    profile = tmp_path / "profile"
    if empty_directory:
        profile.mkdir()
        (profile / "Local State").touch()
    metadata = {}
    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter(
            adapter, "http://127.0.0.1:3001", str(profile), metadata, expected_compatibility={},
        ):
            pytest.fail("Delivered a fresh runtime in place of a saved profile")

    assert failure.value.status == "profile_restore_failed"
    assert metadata["runtime_stopped"] and not metadata["profile_saved"]
    assert profile.exists() is empty_directory
    if empty_directory:
        with ProfileLock(profile):
            pass


async def test_playwright_startup_timeout_stops_driver_and_releases_lock(monkeypatch, tmp_path):
    closed = []

    async def launch(directory, **kwargs):
        assert directory == str(tmp_path)
        assert kwargs["proxy"] == {
            "server": "http://127.0.0.1:3001", "username": "broker", "password": "private",
        }
        assert kwargs["timeout"] == adapters._LAUNCH_TIMEOUT_MS
        assert kwargs["viewport"] == {"width": 390, "height": 844}
        assert kwargs["screen"] == kwargs["viewport"]
        assert "--disable-quic" in kwargs["args"]
        assert kwargs["headless"] is False
        raise adapters.PlaywrightTimeout("private browser argument")

    @asynccontextmanager
    async def playwright():
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
        finally:
            closed.append("driver")

    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setattr(adapters, "async_playwright", playwright)
    metadata = {}

    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter(
            "playwright", "http://127.0.0.1:3001", str(tmp_path), metadata, headless=False,
            device_preset="phone", proxy_auth={"username": "broker", "password": "private"},
        ):
            pytest.fail("Delivered a browser after launch timed out")

    assert failure.value.status == "launch_timeout"
    assert "private browser argument" not in str(failure.value)
    assert closed == ["driver"]
    assert metadata["runtime_stopped"] and not metadata["profile_saved"]
    with ProfileLock(tmp_path):
        pass


@pytest.mark.integration
async def test_playwright_missing_binary_after_inventory_releases_profile_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing-binaries"))
    profile = tmp_path / "profile"
    metadata = {}

    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters.launch_adapter("playwright", "http://127.0.0.1:3001", str(profile), metadata):
            pytest.fail("Delivered a runtime when the browser binary was missing")

    assert failure.value.status == "launch_failed"
    assert "missing-binaries" not in str(failure.value)
    assert metadata["runtime_stopped"] and not metadata["profile_saved"]
    with ProfileLock(profile):
        pass


async def test_playwright_context_close_failure_still_releases_process(monkeypatch):
    closed = []

    async def close_context():
        raise RuntimeError("Context disconnected")

    async def new_context(**_):
        return SimpleNamespace(close=close_context)

    async def close():
        closed.append("browser")

    async def launch(**_):
        return SimpleNamespace(new_context=new_context, close=close)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    monkeypatch.setattr(adapters, "_check_capability", lambda _: None)
    monkeypatch.setattr(adapters, "async_playwright", playwright)
    metadata = {}
    async with adapters.launch_adapter("playwright", "http://127.0.0.1:3001", None, metadata):
        pass

    assert closed == ["browser"]
    assert metadata["runtime_stopped"] and metadata["browser_closed"]


async def test_native_chrome_spawn_failure_removes_temporary_profile(monkeypatch, tmp_path):
    directory = tmp_path / "temporary"
    directory.mkdir()

    async def spawn(*_args, **_kwargs):
        raise FileNotFoundError("private executable path")

    monkeypatch.setattr(adapters, "chrome_path", lambda: "/native/Google Chrome")
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda **_: str(directory))
    monkeypatch.setattr(adapters.asyncio, "create_subprocess_exec", spawn)
    metadata = {}

    with pytest.raises(adapters.AdapterUnavailable) as failure:
        async with adapters._native_chrome("http://127.0.0.1:3001", None, metadata):
            pytest.fail("Delivered a runtime without a Chrome process")

    assert failure.value.status == "launch_failed"
    assert "private executable path" not in str(failure.value)
    assert metadata["runtime_stopped"] and metadata["browser_closed"]
    assert not directory.exists()


async def test_native_manual_chrome_has_no_automation_launcher_flags(monkeypatch, tmp_path, viewport_only_preset):
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
    monkeypatch.setattr(adapters, "adapter_capabilities", lambda: [{"id": "manual_chrome", "available": True}])

    async def authenticate(context, credentials):
        proxy_auth_calls.append(credentials)

    monkeypatch.setattr(adapters, "_proxy_authentication", authenticate)
    metadata = {}
    async with adapters.launch_adapter(
        "manual_chrome", "http://127.0.0.1:3010", str(tmp_path), metadata,
        proxy_auth={"username": "abrl-session", "password": "ephemeral-capability"},
        child_env={"DISPLAY": ":99"},
        batch_preset=viewport_only_preset,
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


def test_manual_registry_checks_for_a_local_display(monkeypatch):
    monkeypatch.setattr(adapters, "chrome_path", lambda: "/installed/google-chrome")
    monkeypatch.setattr(adapters.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    def manual():
        return next(row for row in adapters.adapter_capabilities() if row["id"] == "manual_chrome")

    assert manual()["status"] == "missing_display"
    monkeypatch.setenv("DISPLAY", ":99")
    assert manual()["available"] and "human_control" in manual()["capabilities"]
