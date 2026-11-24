"""Decodo diagnostics use a bounded, exact-origin proxy route and safe evidence."""

import asyncio
import json
import ssl
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from geoip2.errors import AddressNotFoundError

from abrl import network, proxy_preflight
from abrl.proxy import DecodoBinding

LOOKUP = "https://ip.decodo.com"
TARGET = "https://owned.example"
IP_ADDRESS = "8.8.4.4"
SECOND_IP_ADDRESS = "9.9.9.9"


def binding(continuity="sticky"):
    return DecodoBinding(
        "gate.decodo.com", 7000, "residential", "test-user-token", "test-password",
        True, "US", continuity,
    )


async def active():
    return False


def exit_observation(
    ip=IP_ADDRESS, *, country="US", asn=7922, timezone="America/Chicago", tunnel=35, first_byte=190,
):
    return proxy_preflight._Probe(
        ip=ip,
        metadata={"country": country, "asn": asn, "timezone": timezone},
        timings={"tunnel_establish": tunnel, "first_byte": first_byte},
    )


def mock_probes(monkeypatch, selected_binding, *samples):
    results = iter(samples)
    calls = []

    async def probe(assigned, origin, timeout, path):
        assert assigned is selected_binding
        assert 0 < timeout <= proxy_preflight.PROBE_TIMEOUT_SECONDS
        calls.append((origin, path))
        return next(results)

    monkeypatch.setattr(proxy_preflight, "_probe", probe)
    return calls


def geoip(monkeypatch, *, country="US", asn=7922, network_type="residential"):
    def lookup(address, setting, attribute):
        assert address in {IP_ADDRESS, SECOND_IP_ADDRESS}
        assert (setting, attribute) in {
            ("ABRL_GEOIP_CITY_DB", "city"),
            ("ABRL_GEOIP_ASN_DB", "asn"),
            ("ABRL_GEOIP_ANONYMOUS_DB", "anonymous_ip"),
        }
        if attribute == "city":
            return SimpleNamespace(
                country=SimpleNamespace(iso_code=country),
                location=SimpleNamespace(time_zone="America/Chicago"),
            ), "loaded"
        if attribute == "asn":
            return SimpleNamespace(autonomous_system_number=asn), "loaded"
        return SimpleNamespace(
            is_hosting_provider=network_type in {"hosting", "ambiguous"},
            is_residential_proxy=network_type in {"residential", "ambiguous"},
        ), "loaded"

    monkeypatch.setattr(proxy_preflight, "_maxmind", lookup)


async def test_report_uses_confirmed_evidence_and_tunnel_timing_without_addresses(monkeypatch):
    assigned = binding()
    geoip(monkeypatch)
    calls = mock_probes(
        monkeypatch, assigned, exit_observation(), exit_observation(),
        proxy_preflight._Probe(timings={"tunnel_establish": 22}),
    )

    report = await proxy_preflight.run_decodo_preflight(
        assigned, TARGET, {"allowed_asns": [7922], "max_latency_ms": 60}, active,
    )

    assert report["status"] == "passed"
    assert report["blocked"] is False
    assert calls == [(LOOKUP, "/json"), (LOOKUP, "/json"), (TARGET, None)]
    assert report["checks"]["country"]["source"] == "decodo_and_maxmind"
    assert report["checks"]["country"]["dataset_status"] == "loaded"
    assert report["checks"]["asn"]["observed"] == 7922
    assert report["checks"]["sticky"] == {"status": "passed"}
    assert report["checks"]["target_tls"] == {"status": "passed"}
    assert report["checks"]["latency"] == {
        "status": "passed", "threshold_ms": 60, "tunnel_ms": 22, "source": "target_tls",
    }
    assert report["timings_ms"]["lookup"][0]["first_byte"] == 190
    assert report["checks"]["socks5"] == {"status": "not_supported"}
    assert report["checks"]["target_http_80"] == {"status": "not_supported"}
    serialized = json.dumps(report)
    assert IP_ADDRESS not in serialized and "test-password" not in serialized


@pytest.mark.parametrize(
    ("reported_country", "reported_asn", "local_country", "local_asn", "network_type", "reason"),
    [
        ("CA", 7922, "CA", 7922, "residential", "country_mismatch"),
        ("US", 64512, "US", 64512, "residential", "asn_not_allowed"),
        ("US", 7922, "US", 7922, "hosting", "hosting_provider"),
    ],
)
async def test_confirmed_network_mismatches_gate_without_other_probes(
    monkeypatch, reported_country, reported_asn, local_country, local_asn, network_type, reason,
):
    assigned = binding()
    geoip(monkeypatch, country=local_country, asn=local_asn, network_type=network_type)
    calls = mock_probes(
        monkeypatch, assigned, exit_observation(country=reported_country, asn=reported_asn),
    )

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {"allowed_asns": [7922]}, active)

    assert report["reason"] == reason
    assert report["status"] == "blocked" and report["blocked"] is True
    assert calls == [(LOOKUP, "/json")]


@pytest.mark.parametrize("confirmations", [False, True])
async def test_provider_disagreement_or_absence_of_local_evidence_is_report_only(monkeypatch, confirmations):
    assigned = binding("none")
    if confirmations:
        geoip(monkeypatch, network_type="ambiguous")
    else:
        for setting in ("ABRL_GEOIP_CITY_DB", "ABRL_GEOIP_ASN_DB", "ABRL_GEOIP_ANONYMOUS_DB"):
            monkeypatch.delenv(setting, raising=False)
    mock_probes(monkeypatch, assigned, exit_observation(country="CA", asn=64512), proxy_preflight._Probe())

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {"allowed_asns": [7922]}, active)

    assert report["status"] == "partial"
    assert report["blocked"] is False
    assert report["checks"]["sticky"] == {"status": "not_applicable"}
    assert report["checks"]["asn"]["evidence"]["decodo"] == 64512
    if confirmations:
        assert report["checks"]["asn"]["reason"] == "sources_disagree"
        assert report["checks"]["asn"]["evidence"]["maxmind"] == 7922
        assert report["checks"]["asn"]["dataset_status"] == "loaded"
        assert report["checks"]["network_type"]["reason"] == "conflicting_classifications"
    else:
        assert report["checks"]["asn"]["confirmed"] is False
        assert report["checks"]["country"]["reason"] == "unconfirmed_provider_evidence"
        assert report["checks"]["network_type"]["reason"] == "evidence_unavailable"
        assert all(
            report["checks"][name]["dataset_status"] == "not_configured"
            for name in ("country", "asn", "network_type", "timezone")
        )


def test_local_dataset_distinguishes_missing_invalid_unreadable_and_address_not_found(monkeypatch, tmp_path):
    setting = "ABRL_GEOIP_ASN_DB"
    path = tmp_path / "private-dataset.mmdb"
    monkeypatch.setenv(setting, str(path))
    assert proxy_preflight._maxmind(IP_ADDRESS, setting, "asn") == (None, "missing_or_not_file")
    path.write_bytes(b"corrupt dataset")
    assert proxy_preflight._maxmind(IP_ADDRESS, setting, "asn") == (None, "invalid_or_incompatible")

    def unreadable(_):
        raise PermissionError(str(path))

    monkeypatch.setattr(proxy_preflight, "Reader", unreadable)
    assert proxy_preflight._maxmind(IP_ADDRESS, setting, "asn") == (None, "unreadable")

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def asn(self, address):
            raise AddressNotFoundError("not covered by dataset", address, 0)

    monkeypatch.setattr(proxy_preflight, "Reader", lambda _: Reader())
    assert proxy_preflight._maxmind(IP_ADDRESS, setting, "asn") == (None, "address_not_found")


def test_loaded_dataset_with_no_matching_fields_is_distinct_from_no_dataset(monkeypatch):
    geoip(monkeypatch, country=None, asn=None, network_type="unknown")
    checks = proxy_preflight._enrichment(exit_observation())
    for name in ("country", "asn", "network_type"):
        assert checks[name]["status"] == "unavailable"
        assert checks[name]["dataset_status"] == "field_unavailable"


async def test_changed_sticky_address_is_report_only(monkeypatch):
    assigned = binding()
    geoip(monkeypatch)
    mock_probes(
        monkeypatch, assigned, exit_observation(), exit_observation(SECOND_IP_ADDRESS),
        proxy_preflight._Probe(timings={"tunnel_establish": 101}),
    )

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {"max_latency_ms": 100}, active)

    assert report["reason"] == "sticky_ip_changed"
    assert report["checks"]["latency"]["reason"] == "latency_threshold_exceeded"
    assert report["status"] == "partial" and report["blocked"] is False
    assert report["checks"]["sticky"]["exit_evidence"]["asn"]["observed"] == 7922
    assert all(address not in json.dumps(report) for address in (IP_ADDRESS, SECOND_IP_ADDRESS))


async def test_changed_exit_is_evaluated_for_confirmed_mismatch(monkeypatch):
    assigned = binding()
    monkeypatch.setattr(
        proxy_preflight,
        "_maxmind",
        lambda address, _, kind: (
            (SimpleNamespace(autonomous_system_number=7922 if address == IP_ADDRESS else 64512), "loaded")
            if kind == "asn" else (None, "not_configured")
        ),
    )
    calls = mock_probes(
        monkeypatch, assigned, exit_observation(), exit_observation(SECOND_IP_ADDRESS, asn=64512),
    )

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {"allowed_asns": [7922]}, active)

    assert report["blocked"] is True and report["reason"] == "asn_not_allowed"
    assert report["checks"]["sticky"]["reason"] == "sticky_ip_changed"
    assert len(calls) == 2


@pytest.mark.parametrize("reason", ["authentication", "quota", "proxy_destination_denied"])
async def test_gateway_auth_quota_and_destination_denial_are_not_retried(monkeypatch, reason):
    assigned = binding()
    samples = [proxy_preflight._Probe(reason=reason)]
    if reason == "proxy_destination_denied":
        samples.append(proxy_preflight._Probe())
    calls = mock_probes(monkeypatch, assigned, *samples)

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {}, active)

    assert report["blocked"] is (reason == "authentication")
    assert calls.count((LOOKUP, "/json")) == 1
    assert report["checks"]["exit_ip"]["reason"] == reason


async def test_only_one_same_binding_retry_across_probe_stages(monkeypatch):
    assigned = binding()
    geoip(monkeypatch)
    calls = mock_probes(
        monkeypatch, assigned,
        proxy_preflight._Probe(reason="proxy_connect_timeout", retryable=True),
        exit_observation(), exit_observation(),
        proxy_preflight._Probe(reason="origin_tls_timeout", retryable=True),
    )

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {}, active)

    assert calls == [(LOOKUP, "/json")] * 3 + [(TARGET, None)]
    assert report["probe_attempts"]["lookup"][0]["reason"] == "proxy_connect_timeout"
    first, last = report["timings_ms"]["lookup"]
    assert "retry_backoff" not in first
    assert last["retry_backoff"] >= round(proxy_preflight.PROBE_RETRY_BACKOFF_SECONDS * 1000)
    assert report["checks"]["target_tls"]["reason"] == "origin_tls_timeout"
    assert report["status"] == "partial" and report["blocked"] is False


async def test_preflight_cancellation_interrupts_retry_backoff(monkeypatch):
    assigned = binding()
    calls = mock_probes(
        monkeypatch, assigned, proxy_preflight._Probe(reason="proxy_gateway_timeout", retryable=True),
    )
    monkeypatch.setattr(proxy_preflight, "PROBE_RETRY_BACKOFF_SECONDS", 5)

    async def should_cancel():
        return bool(calls)

    report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {}, should_cancel)

    assert report["status"] == "cancelled"
    assert calls == [(LOOKUP, "/json")]
    assert report["probe_attempts"]["lookup"] == [{"reason": "proxy_gateway_timeout"}]


@pytest.mark.parametrize(
    ("target", "policy"),
    [
        ("http://owned.example", {}),
        ("https://owned.example:8443", {}),
        ("https://owned.example/account", {}),
        (TARGET, {"target_origin": "https://other.example"}),
    ],
)
async def test_unsupported_target_does_not_touch_the_provider(monkeypatch, target, policy):
    assigned = binding()
    calls = mock_probes(monkeypatch, assigned)

    report = await proxy_preflight.run_decodo_preflight(assigned, target, policy, active)

    assert report["reason"] == "unsupported_target"
    assert report["status"] == "unavailable" and report["blocked"] is False
    assert calls == []


async def test_unavailable_binding_has_canonical_report_without_network(monkeypatch):
    calls = mock_probes(monkeypatch, None)

    result = await proxy_preflight.run_decodo_preflight(None, TARGET, {}, active)

    assert result == proxy_preflight.unavailable_decodo_preflight()
    assert result["reason"] == "binding_unavailable" and result["blocked"] is False
    assert calls == []


@pytest.mark.parametrize("path", ["/lab/diagnostics/echo", "/lab"])
async def test_fixture_diagnostic_headers_are_scoped_and_proxy_headers_are_removed(path):
    headers = [
        b"User-Agent: Example Browser",
        b"Accept-Language: en-US",
        b'Sec-CH-UA-Platform: "macOS"',
        b"Proxy-Authorization: secret",
        b"Forwarded: for=8.8.4.4",
    ]
    reader = asyncio.StreamReader()
    reader.feed_eof()

    request = await network._fixture_request(reader, "GET", network.FIXTURE_ORIGIN + path, headers)

    assert b"User-Agent" in request
    assert (b"Accept-Language" in request and b"Sec-CH-UA-Platform" in request) == path.endswith("/echo")
    assert b"Proxy-Authorization" not in request and b"Forwarded" not in request


@pytest.mark.parametrize("cancelled", [True, False])
async def test_probe_is_drained_on_cancellation_and_timeout(monkeypatch, cancelled):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def probe(*_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def should_cancel():
        return cancelled and started.is_set()

    monkeypatch.setattr(proxy_preflight, "_probe", probe)
    monkeypatch.setattr(proxy_preflight, "PREFLIGHT_TIMEOUT_SECONDS", 0.25)
    report = await proxy_preflight.run_decodo_preflight(binding(), TARGET, {}, should_cancel)

    assert report["status"] == ("cancelled" if cancelled else "unavailable")
    assert report["timings_ms"]["total"] < 500
    assert stopped.is_set()


@pytest.mark.parametrize(
    ("stage", "reason"),
    [("target_dns", "target_dns_unavailable"), ("gateway_dns", "proxy_gateway_dns_unavailable")],
)
async def test_probe_timeout_attributes_stalled_dns_and_releases_broker(monkeypatch, stage, reason):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def resolve(host, _port):
        if (host == "gate.decodo.com") != (stage == "gateway_dns"):
            return ("8.8.4.4",)
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
    probe = await proxy_preflight._probe(binding(), TARGET, timeout=0.15, path=None)

    assert started.is_set() and stopped.is_set()
    assert probe.reason == reason and probe.retryable
    assert probe.timings[stage] >= 100


@pytest.mark.parametrize(
    ("wire", "reason"),
    [
        (b"HTTP/1.1 409 Conflict\r\n\r\n", "lookup_http_error"),
        (b"HTTP/1.1 302 Found\r\nLocation: https://different.example\r\n\r\n", "lookup_redirect"),
        (b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n", "lookup_http_error"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}", "invalid_lookup_response"),
        (b"HTTP/1.1 200 OK\r\n\r\n" + b" " * 8_193, "lookup_response_too_large"),
        (b'HTTP/1.1 200 OK\r\n\r\n{"proxy":{"ip":"127.0.0.1"}}', "invalid_exit_ip"),
    ],
)
def test_provider_http_response_remains_bounded_and_never_confirms_auth(wire, reason):
    sample = proxy_preflight._Probe()
    proxy_preflight._lookup_payload(wire, sample)
    assert sample.ip is None
    assert sample.reason == reason and not sample.retryable


@pytest.fixture
def local_tls(tmp_path):
    original_context = ssl.create_default_context
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local test certificate")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("ip.decodo.com"), x509.DNSName("gate.decodo.com"), x509.DNSName("owned.example"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    return server, lambda: original_context(cafile=cert_file)


@pytest.mark.parametrize("retry_first_connect", [False, True])
async def test_real_scoped_broker_handshake_times_actual_first_byte_and_sends_no_target_http(
    monkeypatch, local_tls, retry_first_connect,
):
    server_tls, client_tls = local_tls
    assigned = binding("none")
    geoip(monkeypatch)
    connections = []
    lookup_requests = []
    target_data = []
    target_done = asyncio.Event()
    open_connection = asyncio.open_connection

    async def lookup_service(reader, writer):
        try:
            lookup_requests.append(await reader.readuntil(b"\r\n\r\n"))
            body = json.dumps({
                "proxy": {"ip": IP_ADDRESS},
                "country": {"code": "US"},
                "isp": {"asn": 7922},
                "city": {"time_zone": "America/Chicago"},
            }).encode("ascii")
            writer.write(b"H")
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(b"TTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def target_service(reader, writer):
        try:
            target_data.append(await reader.read())
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            target_done.set()

    async def relay(source, destination):
        while chunk := await source.read(2048):
            destination.write(chunk)
            await destination.drain()

    async with (
        await asyncio.start_server(lookup_service, "127.0.0.1", 0, ssl=server_tls) as lookup,
        await asyncio.start_server(target_service, "127.0.0.1", 0, ssl=server_tls) as target,
    ):
        ports = {
            "ip.decodo.com:443": lookup.sockets[0].getsockname()[1],
            "owned.example:443": target.sockets[0].getsockname()[1],
        }

        async def gateway_service(reader, writer):
            destination = None
            pipes = []
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                authority = head.split()[1].decode("ascii")
                connections.append(head)
                if retry_first_connect and len(connections) == 1:
                    writer.write(
                        b"HTTP/1.1 503 Service Unavailable\r\nX-Error-Description: private gateway description\r\n\r\n"
                    )
                    await writer.drain()
                    return
                inbound, destination = await open_connection("127.0.0.1", ports[authority])
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                pipes = [
                    asyncio.create_task(relay(reader, destination)),
                    asyncio.create_task(relay(inbound, writer)),
                ]
                await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pipe in pipes:
                    pipe.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
                if destination is not None:
                    destination.close()
                    await destination.wait_closed()
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

        async with await asyncio.start_server(gateway_service, "127.0.0.1", 0, ssl=server_tls) as gateway:
            async def connect(host, port, **kwargs):
                if host == "127.0.0.1":
                    return await open_connection(host, port, **kwargs)
                assert (host, port, kwargs.get("server_hostname")) == ("93.184.216.34", 7000, "gate.decodo.com")
                return await open_connection("127.0.0.1", gateway.sockets[0].getsockname()[1], **kwargs)

            async def resolve(host, port):
                assert (host, port) in {("ip.decodo.com", 443), ("owned.example", 443), ("gate.decodo.com", 7000)}
                return ("93.184.216.34",)

            monkeypatch.setattr(network, "resolve_public_ipv4", resolve)
            monkeypatch.setattr(asyncio, "open_connection", connect)
            monkeypatch.setattr(ssl, "create_default_context", client_tls)
            report = await proxy_preflight.run_decodo_preflight(assigned, TARGET, {}, active)
            assert report["status"] == "passed", report
            await asyncio.wait_for(target_done.wait(), 1)

    assert [head.split()[1] for head in connections] == [
        *([b"ip.decodo.com:443"] if retry_first_connect else []), b"ip.decodo.com:443", b"owned.example:443",
    ]
    auth = b"Proxy-Authorization: " + assigned.authorization()
    assert all(auth in head for head in connections)
    assert len(lookup_requests) == 1
    assert lookup_requests[0].startswith(b"GET /json HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in lookup_requests[0]
    assert target_data == [b""]
    timings = report["timings_ms"]["lookup"][-1]
    assert timings["first_byte"] + 30 < timings["total"]
    assert all(name in timings for name in ("target_dns", "gateway_tls_connect", "proxy_connect", "origin_tls"))
    if retry_first_connect:
        assert report["probe_attempts"]["lookup"] == [{"reason": "proxy_tunnel_failed"}, {"reason": None}]
        assert timings["retry_backoff"] >= round(proxy_preflight.PROBE_RETRY_BACKOFF_SECONDS * 1000)
        assert "private gateway description" not in json.dumps(report)
    assert "first_byte" not in report["timings_ms"]["target_tls"][0]
