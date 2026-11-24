"""Bounded Decodo transport and network diagnostics for an assigned lease."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import ssl
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPResponse
from io import BytesIO
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from geoip2.database import Reader
from geoip2.errors import AddressNotFoundError
from maxminddb import InvalidDatabaseError

from abrl.network import (
    DECODO_PROXY_CHECK_TARGET,
    EgressGuard,
    NetworkDenied,
    normalize_origin,
    normalize_proxy_check_ip,
)
from abrl.proxy import DECODO_GATEWAY, DECODO_PORT, ProxyBinding

PREFLIGHT_TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 3
PROBE_RETRY_BACKOFF_SECONDS = 0.2
_LOOKUP_BODY_LIMIT = 8192
_LOOKUP_WIRE_LIMIT = 20_480
_TEMPORARY_HTTP_STATUSES = frozenset({500, 502, 503, 504})
_HARD_BLOCK_REASONS = frozenset({"authentication", "country_mismatch", "asn_not_allowed", "hosting_provider"})
LOOKUP_DATASET_STATUSES = (
    "not_configured", "missing_or_not_file", "unreadable", "invalid_or_incompatible",
    "address_not_found", "field_unavailable", "loaded",
)


@dataclass
class _Probe:
    reason: str | None = None
    retryable: bool = False
    timings: dict[str, int | float] = field(default_factory=dict)
    ip: str | None = field(default=None, repr=False)
    metadata: dict = field(default_factory=dict, repr=False)


class _BufferedSocket:
    def __init__(self, data: bytes):
        self.data = data

    def makefile(self, _mode: str) -> BytesIO:
        return BytesIO(self.data)


def _milliseconds(started: float) -> int:
    return round((monotonic() - started) * 1000)


def _lookup_payload(wire: bytes, probe: _Probe) -> None:
    try:
        with HTTPResponse(_BufferedSocket(wire)) as response:
            response.begin()
            if response.status != 200:
                probe.reason = "lookup_redirect" if 300 <= response.status < 400 else "lookup_http_error"
                probe.retryable = response.status in _TEMPORARY_HTTP_STATUSES
                return
            body = response.read(_LOOKUP_BODY_LIMIT + 1)
            if len(body) > _LOOKUP_BODY_LIMIT:
                probe.reason = "lookup_response_too_large"
                return
            if response.length not in {None, 0}:
                probe.reason = "invalid_lookup_response"
                return
        payload = json.loads(body)
    except (HTTPException, ValueError, RecursionError):
        probe.reason = "invalid_lookup_response"
        return
    if not isinstance(payload, dict):
        probe.reason = "invalid_lookup_response"
        return
    ip_data = payload.get("proxy")
    if isinstance(ip_data, dict):
        probe.ip = normalize_proxy_check_ip(ip_data.get("ip"))
    if probe.ip is None:
        probe.reason = "invalid_exit_ip"
        return
    probe.metadata = {
        "country": _nested_field(payload, "country", "code"),
        "asn": _nested_field(payload, "isp", "asn"),
        "timezone": _nested_field(payload, "city", "time_zone"),
    }


def _nested_field(payload: dict, section: str, key: str):
    fields = payload.get(section)
    return fields.get(key) if isinstance(fields, dict) else None


async def _read_lookup(reader, writer, host: str, path: str, started: float, probe: _Probe) -> None:
    writer.write(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nAccept: application/json\r\n"
            "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    first_byte = await reader.read(1)
    if not first_byte:
        probe.reason = "lookup_response_empty"
        probe.retryable = True
        return
    probe.timings["first_byte"] = _milliseconds(started)
    header = first_byte + await reader.readuntil(b"\r\n\r\n")
    if len(header) > _LOOKUP_WIRE_LIMIT:
        probe.reason = "lookup_response_too_large"
        return
    try:
        with HTTPResponse(_BufferedSocket(header)) as response:
            response.begin()
            length = response.length
            if response.status != 200:
                _lookup_payload(header, probe)
                return
    except HTTPException:
        probe.reason = "invalid_lookup_response"
        return
    data = bytearray(header)
    if length is not None:
        if length > _LOOKUP_BODY_LIMIT or len(data) + length > _LOOKUP_WIRE_LIMIT:
            probe.reason = "lookup_response_too_large"
            return
        data.extend(await reader.readexactly(length))
    else:
        while chunk := await reader.read(4096):
            data.extend(chunk)
            if len(data) > _LOOKUP_WIRE_LIMIT:
                probe.reason = "lookup_response_too_large"
                return
    _lookup_payload(bytes(data), probe)


def _transport_failure(guard: EgressGuard, probe: _Probe) -> None:
    if not guard.transport_events:
        return
    event = guard.transport_events[-1]
    probe.reason = event.get("reason") or event["kind"]
    probe.retryable = event["kind"] == "transport" and event.get("retryable") is True


async def _probe(binding: ProxyBinding, origin: str, timeout: float, path: str | None) -> _Probe:
    observation = _Probe()
    guard = EgressGuard(
        origin, [], lambda _: None, upstream=binding,
        traffic_budget_bytes=32_768, max_connections=1, allow_automatic_assets=False,
        diagnostic_timings=observation.timings,
    )
    host = urlsplit(origin).hostname
    started = monotonic()
    phase = "broker_connect"
    try:
        async with asyncio.timeout(timeout), guard:
            broker_port = urlsplit(guard.url).port
            reader, writer = await asyncio.open_connection("127.0.0.1", broker_port, limit=_LOOKUP_WIRE_LIMIT)
            try:
                phase = "proxy_connect"
                writer.write(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode("ascii"))
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
                if not head.startswith(b"HTTP/1.1 200 "):
                    observation.reason = (
                        "destination_unavailable" if head.startswith(b"HTTP/1.1 403 ")
                        else "proxy_tunnel_failed"
                    )
                    return observation
                phase = "origin_tls"
                tls_started = monotonic()
                await writer.start_tls(ssl.create_default_context(), server_hostname=host)
                observation.timings["origin_tls"] = _milliseconds(tls_started)
                observation.timings["connection"] = _milliseconds(started)
                if path is not None:
                    phase = "lookup_response"
                    await _read_lookup(reader, writer, host, path, started, observation)
            finally:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.1)
                except (OSError, TimeoutError):
                    writer.transport.abort()
    except TimeoutError:
        observation.reason = f"{phase}_timeout"
        if phase == "proxy_connect":
            for stage, reason in (
                ("proxy_connect", "proxy_tunnel_timeout"),
                ("gateway_tls_connect", "proxy_gateway_timeout"),
                ("gateway_dns", "proxy_gateway_dns_unavailable"),
                ("target_dns", "target_dns_unavailable"),
            ):
                if stage in observation.timings:
                    observation.reason = reason
                    break
        observation.retryable = True
    except ssl.SSLEOFError:
        observation.reason = f"{phase}_failed"
        observation.retryable = True
    except ssl.SSLError:
        observation.reason = f"{phase}_failed"
    except asyncio.LimitOverrunError:
        observation.reason = (
            "lookup_response_too_large" if phase == "lookup_response" else "proxy_response_too_large"
        )
    except (OSError, asyncio.IncompleteReadError):
        observation.reason = f"{phase}_failed"
        observation.retryable = True
    finally:
        _transport_failure(guard, observation)
        observation.timings["total"] = _milliseconds(started)
    return observation


def _maxmind(address: str, setting: str, lookup: str):
    """Look up the address locally and retain only a fixed dataset status."""
    path = os.environ.get(setting)
    if not path:
        return None, "not_configured"
    try:
        if not Path(path).is_file():
            return None, "missing_or_not_file"
        with Reader(path) as reader:
            return getattr(reader, lookup)(address), "loaded"
    except AddressNotFoundError:
        return None, "address_not_found"
    except FileNotFoundError:
        return None, "missing_or_not_file"
    except OSError:
        return None, "unreadable"
    except (InvalidDatabaseError, TypeError, ValueError):
        return None, "invalid_or_incompatible"


def _country(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{2}", value) else None


def _asn(value) -> int | None:
    return value if type(value) is int and 1 <= value <= 4_294_967_295 else None


def _timezone(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", value):
        return None
    try:
        return ZoneInfo(value).key
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _enrichment(probe: _Probe) -> dict:
    city, city_status = _maxmind(probe.ip, "ABRL_GEOIP_CITY_DB", "city")
    asn, asn_status = _maxmind(probe.ip, "ABRL_GEOIP_ASN_DB", "asn")
    classification, classification_status = _maxmind(probe.ip, "ABRL_GEOIP_ANONYMOUS_DB", "anonymous_ip")
    checks = {}
    for name, validate, local, dataset_status in (
        ("country", _country, city.country.iso_code if city else None, city_status),
        ("asn", _asn, asn.autonomous_system_number if asn else None, asn_status),
        ("timezone", _timezone, city.location.time_zone if city else None, city_status),
    ):
        decodo_value = validate(probe.metadata.get(name))
        local_value = validate(local)
        evidence = {
            source: value
            for source, value in (("decodo", decodo_value), ("maxmind", local_value))
            if value is not None
        }
        if not evidence:
            checks[name] = {"status": "unavailable", "reason": "evidence_unavailable"}
        elif len(set(evidence.values())) > 1:
            checks[name] = {"status": "unavailable", "reason": "sources_disagree", "evidence": evidence}
        elif local_value is None:
            checks[name] = {
                "status": "unavailable", "reason": "unconfirmed_provider_evidence",
                "observed": decodo_value, "source": "decodo", "confirmed": False, "evidence": evidence,
            }
        else:
            checks[name] = {
                "status": "passed", "observed": local_value, "confirmed": True,
                "source": "decodo_and_maxmind" if decodo_value is not None else "maxmind", "evidence": evidence,
            }
        checks[name]["dataset_status"] = (
            "field_unavailable" if dataset_status == "loaded" and local_value is None else dataset_status
        )
    hosting = classification and classification.is_hosting_provider is True
    residential = classification and classification.is_residential_proxy is True
    if hosting and residential:
        checks["network_type"] = {"status": "unavailable", "reason": "conflicting_classifications"}
    elif hosting or residential:
        checks["network_type"] = {
            "status": "passed",
            "observed": "hosting" if hosting else "residential",
            "source": "maxmind",
        }
    else:
        checks["network_type"] = {"status": "unavailable", "reason": "evidence_unavailable"}
    checks["network_type"]["dataset_status"] = (
        "field_unavailable" if classification_status == "loaded" and not (hosting or residential)
        else classification_status
    )
    return checks


def _report() -> dict:
    return {
        "status": "unavailable",
        "blocked": False,
        "reason": None,
        "checks": {
            **{
                name: {"status": "not_run"}
                for name in ("exit_ip", "country", "asn", "timezone", "network_type", "sticky", "target_tls", "latency")
            },
            "socks5": {"status": "not_supported"},
            "target_http_80": {"status": "not_supported"},
        },
        "timings_ms": {"lookup": [], "sticky_lookup": [], "target_tls": [], "total": 0},
        "probe_attempts": {"lookup": [], "sticky_lookup": [], "target_tls": []},
    }


def unavailable_decodo_preflight() -> dict:
    """Return the peer report used when the caller could not load the binding."""
    return {**_report(), "reason": "binding_unavailable"}


def _finish(report: dict, status: str, reason: str | None = None) -> dict:
    report.update(status=status, blocked=status == "blocked", reason=reason)
    return report


def _enforce_evidence(checks: dict, policy: dict) -> str | None:
    checks["country"]["expected"] = "US"
    required_asns = policy.get("allowed_asns", ())
    country = checks["country"].get("observed") if checks["country"].get("confirmed") else None
    asn = checks["asn"].get("observed") if checks["asn"].get("confirmed") else None
    for check, mismatched, reason in (
        ("country", country is not None and country != "US", "country_mismatch"),
        ("asn", bool(required_asns and asn is not None and asn not in required_asns), "asn_not_allowed"),
        ("network_type", checks["network_type"].get("observed") == "hosting", "hosting_provider"),
    ):
        if mismatched:
            checks[check].update(status="failed", reason=reason)
            return reason
    return None


async def _collect(report: dict, binding: ProxyBinding, target: str, policy: dict, deadline: float) -> dict:
    retry_available = True
    checks = report["checks"]
    lookup_origin, lookup_path = DECODO_PROXY_CHECK_TARGET

    async def sample(name, origin, path=None):
        nonlocal retry_available
        backoff_ms = None
        while (remaining := deadline - monotonic()) > 0:
            observed = await _probe(binding, origin, min(PROBE_TIMEOUT_SECONDS, remaining), path)
            if backoff_ms is not None:
                observed.timings["retry_backoff"] = backoff_ms
            report["timings_ms"][name].append(observed.timings)
            report["probe_attempts"][name].append({"reason": observed.reason})
            if (
                not observed.retryable or not retry_available
                or deadline - monotonic() <= PROBE_RETRY_BACKOFF_SECONDS
            ):
                return observed
            retry_available = False
            started = monotonic()
            await asyncio.sleep(PROBE_RETRY_BACKOFF_SECONDS)
            backoff_ms = _milliseconds(started)
        return _Probe(reason="preflight_timeout")

    lookup = await sample("lookup", lookup_origin, lookup_path)
    if lookup.reason in _HARD_BLOCK_REASONS:
        checks["exit_ip"] = {"status": "failed", "reason": lookup.reason}
        return _finish(report, "blocked", lookup.reason)
    if lookup.ip is None:
        checks["exit_ip"] = {"status": "unavailable", "reason": lookup.reason}
    else:
        checks["exit_ip"] = {"status": "passed"}
        checks.update(await asyncio.to_thread(_enrichment, lookup))
        if reason := _enforce_evidence(checks, policy):
            return _finish(report, "blocked", reason)

    sticky = None
    if binding.continuity != "sticky":
        checks["sticky"] = {"status": "not_applicable"}
    elif lookup.ip is None:
        checks["sticky"] = {"status": "unavailable", "reason": "first_lookup_unavailable"}
    else:
        sticky = await sample("sticky_lookup", lookup_origin, lookup_path)
        if sticky.reason in _HARD_BLOCK_REASONS:
            checks["sticky"] = {"status": "failed", "reason": sticky.reason}
            return _finish(report, "blocked", sticky.reason)
        if sticky.ip is None:
            checks["sticky"] = {"status": "unavailable", "reason": sticky.reason}
        elif sticky.ip != lookup.ip:
            checks["sticky"] = {"status": "failed", "reason": "sticky_ip_changed"}
            evidence = await asyncio.to_thread(_enrichment, sticky)
            checks["sticky"]["exit_evidence"] = evidence
            if reason := _enforce_evidence(evidence, policy):
                return _finish(report, "blocked", reason)
        else:
            checks["sticky"] = {"status": "passed"}

    target_probe = None
    if lookup.reason == "quota" or (sticky and sticky.reason == "quota"):
        checks["target_tls"] = {"status": "not_run", "reason": "quota"}
    else:
        target_probe = await sample("target_tls", target)
        if target_probe.reason:
            checks["target_tls"] = {"status": "failed", "reason": target_probe.reason}
            if target_probe.reason in _HARD_BLOCK_REASONS:
                return _finish(report, "blocked", target_probe.reason)
        else:
            checks["target_tls"] = {"status": "passed"}

    threshold = policy.get("max_latency_ms")
    if type(threshold) not in {int, float} or not math.isfinite(threshold) or threshold <= 0:
        threshold = None
    target_latency = target_probe.timings.get("tunnel_establish") if target_probe else None
    tunnel_ms = target_latency if target_latency is not None else lookup.timings.get("tunnel_establish")
    if tunnel_ms is None:
        checks["latency"] = {"status": "unavailable", "reason": "tunnel_timing_unavailable"}
    elif threshold is None:
        checks["latency"] = {"status": "observed", "tunnel_ms": tunnel_ms}
    elif tunnel_ms > threshold:
        checks["latency"] = {
            "status": "failed", "reason": "latency_threshold_exceeded",
            "tunnel_ms": tunnel_ms, "threshold_ms": threshold,
        }
    else:
        checks["latency"] = {"status": "passed", "tunnel_ms": tunnel_ms, "threshold_ms": threshold}
    if tunnel_ms is not None:
        checks["latency"]["source"] = "target_tls" if target_latency is not None else "lookup"

    if lookup.ip is None and checks["target_tls"]["status"] != "passed":
        return _finish(report, "unavailable", lookup.reason)
    for name in ("exit_ip", "target_tls", "sticky", "latency", "country", "asn", "network_type", "timezone"):
        check = checks[name]
        if check["status"] in {"failed", "unavailable", "not_run"}:
            return _finish(report, "partial", check.get("reason"))
    return _finish(report, "passed")


async def run_decodo_preflight(
    binding: ProxyBinding | None,
    target_origin: str,
    policy: dict,
    cancelled: Callable[[], Awaitable[bool]],
) -> dict:
    """Inspect this exact binding, retaining only validated evidence and timings."""
    if binding is None or (
        binding.provider != "decodo" or binding.host != DECODO_GATEWAY or binding.port != DECODO_PORT
        or binding.product != "residential" or binding.country != "US" or not binding.provider_resolved_ack
    ):
        return unavailable_decodo_preflight()
    report = _report()
    started = monotonic()
    try:
        origin = normalize_origin(target_origin)
        if not origin.startswith("https://") or policy.get("target_origin") not in {None, origin}:
            raise NetworkDenied("Target is outside the probe scope")
    except NetworkDenied:
        return _finish(report, "unavailable", "unsupported_target")
    if await cancelled():
        return _finish(report, "cancelled", "cancelled")

    async def wait_for_cancel():
        while not await cancelled():
            await asyncio.sleep(0.1)

    deadline = started + PREFLIGHT_TIMEOUT_SECONDS
    probe = asyncio.create_task(_collect(report, binding, origin, policy, deadline))
    watcher = asyncio.create_task(wait_for_cancel())
    try:
        done, _ = await asyncio.wait(
            (probe, watcher), timeout=max(0, deadline - monotonic()), return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher in done:
            watcher.result()
            return _finish(report, "cancelled", "cancelled")
        if probe in done:
            return probe.result()
        status = "partial" if report["checks"]["exit_ip"]["status"] == "passed" else "unavailable"
        return _finish(report, status, "preflight_timeout")
    finally:
        for task in (probe, watcher):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        report["timings_ms"]["total"] = _milliseconds(started)
