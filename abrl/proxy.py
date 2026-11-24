"""Explainable transport-only proxy selection and secret-safe provider bindings."""

from __future__ import annotations

import base64
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

PROVIDERS = {"oxylabs", "decodo"}
PRODUCTS = {"residential", "isp", "dedicated_isp", "datacenter", "dedicated_datacenter"}
FIXED_PRODUCTS = PRODUCTS - {"residential"}
DECODO_GATEWAY = "gate.decodo.com"
DECODO_PORT = 7000
MAX_PROXY_CONNECTIONS = 100
FIXED_GATEWAYS = {
    "isp": "isp.oxylabs.io",
    "dedicated_isp": "disp.oxylabs.io",
    "datacenter": "dc.oxylabs.io",
    "dedicated_datacenter": "ddc.oxylabs.io",
}
_REF = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9]{1,40}\Z")
_OXYLABS_HOST = re.compile(r"(?:[a-z0-9-]+\.)*oxylabs\.io\Z")


class ProxyConfigurationError(ValueError):
    """Safe configuration error: never include a credential in the message."""


def proxy_scope_allows_target(target_origin: str | None, target: Mapping) -> bool:
    if target_origin is None:
        return True
    return {
        target.get("origin"),
        *target.get("navigation_origins", []),
        *target.get("resource_origins", []),
    } == {target_origin}


def product_capabilities(config: dict) -> dict:
    """Capabilities of documented gateway modes, not claims of measured uptime.

    This integration uses the residential username-session gateway or an assigned
    fixed self-service port. Rotating port 8000 and enterprise IP endpoints need
    their own contracts and cannot masquerade as fixed inventory here.
    """
    provider, product = config.get("provider", "oxylabs"), config.get("product")
    host = str(config.get("host", "")).lower()
    port = config.get("port")
    valid_port = isinstance(port, int) and not isinstance(port, bool)
    oxylabs_residential = (
        provider == "oxylabs" and product == "residential" and host == "pr.oxylabs.io" and port == 7777
    )
    decodo_residential = (
        provider == "decodo"
        and product == "residential"
        and host == DECODO_GATEWAY
        and valid_port
        and port == DECODO_PORT
        and config.get("country") == "US"
    )
    residential = oxylabs_residential or decodo_residential
    fixed = (
        provider == "oxylabs"
        and product in FIXED_GATEWAYS
        and host == FIXED_GATEWAYS.get(product)
        and valid_port
        and 8001 <= port <= 65535
    )
    return {
        "supported": bool(residential or fixed),
        "supports_sticky": residential,
        "supports_strict_affinity": oxylabs_residential,
        "fixed_exit": bool(fixed),
        "supports_exclusivity": bool(fixed and product in {"dedicated_isp", "dedicated_datacenter"}),
        "max_session_minutes": 1440 if residential else None,
        "basis": "documented_gateway_mode",
    }


def validate_inventory_config(config: dict) -> dict:
    provider = config.get("provider", "oxylabs")
    if provider not in PROVIDERS:
        raise ProxyConfigurationError("Unknown proxy provider")
    capabilities = product_capabilities(config)
    if not capabilities["supported"]:
        if provider == "decodo":
            raise ProxyConfigurationError("Decodo supports US Residential at gate.decodo.com:7000")
        raise ProxyConfigurationError(
            "Use pr.oxylabs.io:7777 for residential, or the product's assigned fixed port (8001+) at isp/disp/dc/ddc.oxylabs.io; rotating port 8000 and custom gateways are unsupported"
        )
    if config.get("exclusive") and not capabilities["supports_exclusivity"]:
        raise ProxyConfigurationError(
            "Exclusive inventory requires a dedicated ISP or dedicated datacenter product"
        )
    return capabilities


def lease_proxy_config(inventory: dict, policy: dict, session_key: str) -> dict:
    """Resolve policy into a supported binding without reading provider secrets."""
    capabilities = validate_inventory_config(inventory)
    if inventory.get("provider", "oxylabs") != policy.get("provider", "oxylabs"):
        raise ProxyConfigurationError("Proxy inventory does not belong to the selected provider")
    continuity = policy.get("continuity", "none")
    config = {**inventory, "provider_resolved_ack": policy.get("provider_resolved_ack", False)}
    load = policy.get("connection_load", 1)
    if type(load) is not int or not 1 <= load <= MAX_PROXY_CONNECTIONS:
        raise ProxyConfigurationError(f"Connection load must be an integer from 1 to {MAX_PROXY_CONNECTIONS}")
    config["max_connections"] = load
    if inventory["product"] == "residential":
        if continuity in {"fixed", "exclusive"} or policy.get("exclusive"):
            raise ProxyConfigurationError("Residential sessions do not supply fixed or exclusive inventory")
        if continuity == "strict" and not capabilities["supports_strict_affinity"]:
            raise ProxyConfigurationError("This provider does not support strict affinity")
        if continuity in {"sticky", "strict"}:
            duration = policy.get("continuity_minutes", policy.get("duration_minutes", 60))
            if (
                not isinstance(duration, int)
                or isinstance(duration, bool)
                or not 1 <= duration <= capabilities["max_session_minutes"]
            ):
                raise ProxyConfigurationError("Residential session duration must be 1–1440 minutes")
            if duration > inventory.get("max_duration_minutes", 0):
                raise ProxyConfigurationError("Requested continuity exceeds configured inventory duration")
            if not _TOKEN.fullmatch(session_key):
                raise ProxyConfigurationError("Invalid residential session token")
            config.update(
                session_key=session_key, strict_affinity=continuity == "strict", ttl_minutes=duration
            )
        else:
            for field_name in ("session_key", "strict_affinity", "ttl_minutes"):
                config.pop(field_name, None)
    return config


def _number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    return result if math.isfinite(result) else default


def classify_proxy_failure(status: int | None, description: str = "", *, action_started=False) -> dict:
    """Use the upstream CONNECT response only, never a site's HTTP response."""
    description = description.lower()
    quota = bool(re.search(r"\b(?:quota|traffic limit|insufficient balance|balance exhausted)\b", description))
    authentication = any(
        phrase in description
        for phrase in ("invalid credentials", "authentication failed", "proxy authentication required")
    )
    if status in {401, 407} or quota or authentication:
        return {"kind": "quota" if quota else "authentication", "action": "pause_pool", "retryable": False}
    if status in {502, 503, 504} or status is None:
        return {
            "kind": "transport",
            "action": "needs_reconciliation" if action_started else "replace_at_checkpoint",
            "retryable": not action_started,
        }
    return {"kind": "upstream_rejected", "action": "pause_assignment", "retryable": False}


def select_proxy(policy: dict, candidates: list[dict], existing: dict | None = None) -> dict:
    """Rank eligible inventory without inventing health or cost observations.

    The result is a decision, not a reservation. The controller must atomically
    reserve capacity and re-run selection if that reservation loses a race.
    """
    provider = policy.get("provider", "oxylabs")
    if provider not in PROVIDERS:
        raise ProxyConfigurationError("Unknown proxy provider")
    allowed = set(policy.get("allowed_products", policy.get("products", PRODUCTS)))
    if not allowed <= PRODUCTS:
        raise ProxyConfigurationError("Unknown proxy product family")
    region = policy.get("region", policy.get("country"))
    fallbacks = policy.get("fallback_regions", policy.get("fallback_countries", []))
    regions = [str(r).upper() for r in [region, *fallbacks] if r]
    continuity = policy.get("continuity", "none")
    required_duration = _number(policy.get("duration_minutes", policy.get("continuity_minutes")), 0)
    max_latency = _number(policy.get("max_latency_ms"))
    min_success = _number(policy.get("min_transport_success_rate"))
    rejected, eligible = [], []
    now = datetime.now(timezone.utc)
    pool = list(candidates)
    if existing and not any(c.get("id") == existing.get("id") for c in pool):
        pool.append(existing)
    for candidate in pool:
        reasons = []
        product = candidate.get("product")
        capabilities = product_capabilities(candidate)
        if candidate.get("provider", "oxylabs") != provider:
            reasons.append("provider_not_permitted")
        if not capabilities["supported"]:
            reasons.append("unsupported_product_gateway")
        if candidate.get("account_capacity_exhausted"):
            reasons.append("account_capacity")
        if candidate.get("transport_replacement_excluded"):
            reasons.append("fixed_binding_failed_before_action")
        candidate_region = str(candidate.get("region", candidate.get("country", ""))).upper()
        if product not in allowed:
            reasons.append("product_not_permitted")
        if regions and candidate_region not in regions:
            reasons.append("geography_not_permitted")
        if (
            candidate.get("status", "active") not in {"active", "healthy"}
            or candidate.get("entitled", True) is not True
        ):
            reasons.append("pool_unavailable")
        if continuity in {"fixed", "exclusive"} and not capabilities["fixed_exit"]:
            reasons.append("fixed_exit_required")
        if continuity in {"sticky", "strict"} and product == "residential":
            if not capabilities["supports_sticky"]:
                reasons.append("sticky_session_required")
            if continuity == "strict" and not capabilities["supports_strict_affinity"]:
                reasons.append("strict_affinity_required")
            if (
                existing
                and existing.get("id") == candidate.get("id")
                and candidate.get("session_key")
                and continuity == "strict"
                and not candidate.get("strict_affinity")
            ):
                reasons.append("existing_binding_is_not_strict")
        if (policy.get("exclusive") or continuity == "exclusive") and not (
            candidate.get("exclusive") and capabilities["supports_exclusivity"]
        ):
            reasons.append("exclusive_inventory_required")
        remaining = _number(candidate.get("remaining_minutes", candidate.get("max_duration_minutes")))
        if required_duration and (remaining is None or remaining < required_duration):
            reasons.append("insufficient_confirmed_duration")
        if candidate.get("expires_at"):
            try:
                expiry = datetime.fromisoformat(candidate["expires_at"].replace("Z", "+00:00"))
                if expiry.tzinfo is None or expiry <= now:
                    reasons.append("binding_expired")
            except (ValueError, TypeError):
                reasons.append("invalid_expiry")
        active = _number(candidate.get("active_connections"), 0)
        capacity = _number(candidate.get("max_connections", candidate.get("capacity")), 1)
        required = max(1, _number(policy.get("connection_load"), 1))
        held = existing is not None and existing.get("id") == candidate.get("id")
        if capacity <= 0 or active + (0 if held else required) > capacity:
            reasons.append("capacity_exhausted")
        latency = _number(candidate.get("latency_ms"))
        success = _number(candidate.get("transport_success_rate"))
        if max_latency is not None and (latency is None or latency > max_latency):
            reasons.append("latency_threshold_unmet")
        if min_success is not None and (success is None or success < min_success):
            reasons.append("transport_threshold_unmet")
        if (
            policy.get("adapter")
            and candidate.get("adapters")
            and policy["adapter"] not in candidate["adapters"]
        ):
            reasons.append("adapter_incompatible")
        if reasons:
            rejected.append({"id": candidate.get("id"), "reasons": reasons})
            continue
        # An unmeasured candidate is eligible only when thresholds allow it, and
        # follows a candidate with measured transport health. No detector input.
        rank = (
            regions.index(candidate_region) if regions else 0,
            success is None,
            -(success or 0),
            latency is None,
            latency or 0,
            active / capacity,
            _number(candidate.get("cost_per_gb")) is None,
            _number(candidate.get("cost_per_gb"), 0),
            str(candidate.get("id", "")),
        )
        eligible.append((rank, candidate))
    rationale = []
    chosen = None
    if existing and continuity != "none":
        chosen = next((c for _, c in eligible if c.get("id") == existing.get("id")), None)
        if chosen:
            rationale.append("Preserved eligible healthy binding for continuity")
    if not chosen and eligible:
        chosen = min(eligible, key=lambda entry: entry[0])[1]
        rationale.append(
            "Ranked by permitted region, measured transport reliability, latency, load, and known cost"
        )
    if chosen:
        rationale.append("Provider verdicts and site challenges are excluded from transport health")
        if chosen["product"] == "residential":
            rationale.append(
                "Strict affinity fails if the exit disappears; it does not guarantee uptime or an exclusive IP"
                if continuity == "strict"
                else "Residential sticky exits may change before the configured lifetime; fixed continuity requires fixed inventory"
            )
    direct = (
        not chosen
        and provider == "oxylabs"
        and bool(policy.get("allow_direct", False))
        and continuity == "none"
        and not regions
    )
    return {
        "status": "selected" if chosen else "direct" if direct else "unavailable",
        "candidate": dict(chosen) if chosen else None,
        "rationale": rationale
        or ["Explicit direct fallback" if direct else "No candidate meets the policy"],
        "rejected": rejected,
        "evidence": (
            {
                k: chosen.get(k)
                for k in (
                    "transport_success_rate",
                    "transport_samples",
                    "latency_ms",
                    "active_connections",
                    "max_connections",
                    "cost_per_gb",
                    "health_observed_at",
                )
            }
            if chosen
            else {}
        ),
    }


def _secret(reference: str) -> str:
    if not isinstance(reference, str) or not _REF.fullmatch(reference.upper()):
        raise ProxyConfigurationError("Proxy credentials require named secret references")
    value = os.environ.get("ABRL_SECRET_" + reference.upper())
    if not value or any(c in value for c in "\r\n\x00"):
        raise ProxyConfigurationError("A proxy credential reference is missing or invalid")
    return value


@dataclass(frozen=True)
class ProxyBinding:
    provider: ClassVar[str] = "oxylabs"
    host: str
    port: int
    product: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    provider_resolved_ack: bool = False
    country: str | None = None
    continuity: str = "none"
    ttl_minutes: int | None = None

    @classmethod
    def from_config(cls, config: dict) -> ProxyBinding:
        provider = config.get("provider", "oxylabs")
        if provider == "oxylabs":
            return OxylabsBinding.from_config(config)
        if provider == "decodo":
            return DecodoBinding.from_config(config)
        raise ProxyConfigurationError("Unknown proxy provider")

    def authorization(self) -> bytes:
        return b"Basic " + base64.b64encode(f"{self.username}:{self.password}".encode())

    def public_metadata(self) -> dict:
        return {
            "provider": self.provider,
            "product": self.product,
            "country": self.country,
            "continuity": self.continuity,
            "configured_ttl_minutes": self.ttl_minutes,
            "continuity_guarantee": "Failure instead of silent replacement; uptime and exclusivity not guaranteed"
            if self.continuity == "strict"
            else "May replace exit before expiry"
            if self.continuity in {"sticky", "none"}
            else "Assigned fixed inventory port; availability depends on provider",
            "dns_mode": "provider_resolved",
            "dns_boundary": "Gateway resolves the allowed hostname; local target IP pinning is unavailable",
        }


class OxylabsBinding(ProxyBinding):
    @classmethod
    def from_config(cls, config: dict) -> OxylabsBinding:
        if any(name in config for name in ("password", "username", "url")):
            raise ProxyConfigurationError("Use credential references, not inline proxy credentials")
        product, host = config.get("product"), str(config.get("host", "")).lower()
        if product not in PRODUCTS or not _OXYLABS_HOST.fullmatch(host):
            raise ProxyConfigurationError("Select an Oxylabs product and its documented gateway hostname")
        port = config.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProxyConfigurationError("Invalid proxy gateway port")
        validate_inventory_config(config)
        if config.get("provider_resolved_ack") is not True:
            raise ProxyConfigurationError(
                "Acknowledge upstream DNS resolution before using a provider proxy"
            )
        username, password = _secret(config.get("username_ref")), _secret(config.get("password_ref"))
        if ":" in username:
            raise ProxyConfigurationError("Invalid proxy username")
        if product == "residential":
            session_prefix = "sessid_oneip" if config.get("strict_affinity") else "sessid"
            for key, prefix in (("country", "cc"), ("session_key", session_prefix)):
                value = config.get(key)
                if value:
                    if not _TOKEN.fullmatch(str(value)):
                        raise ProxyConfigurationError("Invalid residential session or region token")
                    username += f"-{prefix}-{value}"
            if config.get("strict_affinity"):
                if not config.get("session_key"):
                    raise ProxyConfigurationError("Strict affinity requires a session key")
            ttl = config.get("ttl_minutes")
            if ttl is not None:
                if not config.get("session_key"):
                    raise ProxyConfigurationError("Residential duration requires a session key")
                if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 1440:
                    raise ProxyConfigurationError("Residential session duration must be 1–1440 minutes")
                username += f"-sesstime-{ttl}"
        continuity = (
            "strict" if config.get("strict_affinity") else "sticky" if config.get("session_key") else "none"
        )
        return cls(
            host,
            port,
            product,
            username,
            password,
            True,
            config.get("country"),
            continuity if product == "residential" else "fixed",
            config.get("ttl_minutes"),
        )


class DecodoBinding(ProxyBinding):
    provider = "decodo"

    @classmethod
    def from_config(cls, config: dict) -> DecodoBinding:
        if any(name in config for name in ("password", "username", "url")):
            raise ProxyConfigurationError("Use credential references, not inline proxy credentials")
        if config.get("provider") != "decodo":
            raise ProxyConfigurationError("Select the Decodo proxy provider")
        validate_inventory_config(config)
        if config.get("provider_resolved_ack") is not True:
            raise ProxyConfigurationError(
                "Acknowledge upstream DNS resolution before using a provider proxy"
            )
        if config.get("strict_affinity"):
            raise ProxyConfigurationError("Decodo Residential does not support strict affinity")
        raw_username = _secret(config.get("username_ref"))
        password = _secret(config.get("password_ref"))
        # Keep the secret reference to the dashboard username. Provider routing
        # parameters are added here from the platform's validated configuration.
        if not re.fullmatch(r"[A-Za-z0-9_]+", raw_username):
            raise ProxyConfigurationError("Use the raw Decodo proxy username without routing parameters")
        username = f"user-{raw_username}-country-us"
        session_key = config.get("session_key")
        if session_key:
            if not isinstance(session_key, str) or not _TOKEN.fullmatch(session_key):
                raise ProxyConfigurationError("Invalid residential session token")
            username += f"-session-{session_key}"
        ttl = config.get("ttl_minutes")
        if ttl is not None:
            if not session_key:
                raise ProxyConfigurationError("Residential duration requires a session key")
            if type(ttl) is not int or not 1 <= ttl <= 1440:
                raise ProxyConfigurationError("Residential session duration must be 1–1440 minutes")
            username += f"-sessionduration-{ttl}"
        return cls(
            DECODO_GATEWAY,
            DECODO_PORT,
            "residential",
            username,
            password,
            True,
            "US",
            "sticky" if session_key else "none",
            ttl,
        )

    def public_metadata(self) -> dict:
        return {
            **super().public_metadata(),
            "continuity_guarantee": "May replace exit before expiry"
            if self.continuity == "sticky"
            else "Rotating residential exit",
        }
